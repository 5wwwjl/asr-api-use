import asyncio
import base64
import json
import sys
import threading
from array import array
from pathlib import Path

import pytest
from aiohttp import WSMsgType
from aiohttp.client_exceptions import ClientConnectionResetError


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

import asr_bridge  # noqa: E402
from asr_bridge import BridgeSession, log_business_asr_event  # noqa: E402
from call_state import CallHoldState  # noqa: E402
from hotword_manager import HotwordManager, HotwordStage  # noqa: E402
from turn_recording_coordinator import CompletedTurnRecording, TurnRecordingCoordinator  # noqa: E402


@pytest.fixture(autouse=True)
def clear_global_turn_state():
    asr_bridge._global_speech_events.clear()
    asr_bridge._global_pending_completed_texts.clear()
    asr_bridge._model_preferences.clear()
    asr_bridge._turn_recording_coordinator = TurnRecordingCoordinator()
    asr_bridge._call_hold_state = CallHoldState()
    yield
    asr_bridge._global_speech_events.clear()
    asr_bridge._global_pending_completed_texts.clear()
    asr_bridge._model_preferences.clear()
    asr_bridge._turn_recording_coordinator = TurnRecordingCoordinator()
    asr_bridge._call_hold_state = CallHoldState()


def test_business_asr_log_routes_addressbot_and_firebot(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_bridge, "ASR_BUSINESS_LOG_DIR", str(tmp_path), raising=False)
    for logger in asr_bridge._BUSINESS_LOGGERS.values():
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    asr_bridge._BUSINESS_LOGGERS.clear()

    log_business_asr_event({
        "project": "addressbot",
        "event": "speech.final",
        "callId": "call-address",
        "text": "我在科兴科学园",
        "provider": "xfyun",
        "providers": ["xfyun"],
    })
    log_business_asr_event({
        "event": "call.corrected",
        "callId": "call-fire",
        "correctedText": "深圳湾消防站",
        "correctionProvider": "db_align+llm_highlight",
        "turns": [{
            "segmentId": "caller-0001",
            "correctedText": "深圳湾消防站",
            "keywords": ["深圳湾消防站", "火灾"],
        }],
        "replacements": [{"original": "深圳湾消防战", "corrected": "深圳湾消防站"}],
    })

    addressbot_log = tmp_path / "addressbot.log"
    firebot_log = tmp_path / "firebot.log"
    assert addressbot_log.exists()
    assert firebot_log.exists()
    assert '"project":"addressbot"' in addressbot_log.read_text(encoding="utf-8")
    address_log = json.loads(addressbot_log.read_text(encoding="utf-8").splitlines()[-1].split(" ", 2)[2])
    assert address_log["provider"] == "xfyun"
    assert address_log["providers"] == ["xfyun"]
    assert "我在科兴科学园" in addressbot_log.read_text(encoding="utf-8")
    assert '"project":"firebot"' in firebot_log.read_text(encoding="utf-8")
    assert "深圳湾消防站" in firebot_log.read_text(encoding="utf-8")
    correction_log = json.loads(firebot_log.read_text(encoding="utf-8").splitlines()[-1].split(" ", 2)[2])
    assert correction_log["correctionProvider"] == "db_align+llm_highlight"
    assert correction_log["turns"][0]["keywords"] == ["深圳湾消防站", "火灾"]
    assert correction_log["replacements"][0]["corrected"] == "深圳湾消防站"


class FakeMessage:
    type = WSMsgType.TEXT

    def __init__(self, data: str):
        self.data = data


class FakeUpstreamWs:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeClientWs:
    def __init__(self):
        self.sent = []

    async def send_str(self, payload: str):
        self.sent.append(json.loads(payload))


def test_streaming_text_is_broadcast_to_monitor_and_forwarded_to_client(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    upstream = FakeUpstreamWs([
        FakeMessage(json.dumps({
            "mode": "streaming",
            "text": "喂喂喂，这里是119。",
            "is_final": True,
            "wav_name": "call-1",
        }, ensure_ascii=False))
    ])
    client = FakeClientWs()
    session = BridgeSession(client, upstream, "call-1", callfrom="8001", callto="8002")
    session.speaker = "agent"
    session.direction = "outbound"
    session.turn_start_ms = 100
    session.turn_end_ms = 1500
    session.current_segment_id = "agent-0001"
    session._valid_segment_ids.add("agent-0001")

    asyncio.run(session._upstream_to_client())

    assert client.sent == [{
        "mode": "streaming",
        "text": "喂喂喂，这里是119。",
        "is_final": False,
        "callId": "call-1",
        "segmentId": "agent-0001",
    }]
    assert len(events) == 1
    event, kwargs = events[0]
    assert event["event"] == "speech.final"
    assert event["callId"] == "call-1"
    assert event["callfrom"] == "8001"
    assert event["callto"] == "8002"
    assert event["speaker"] == "agent"
    assert event["text"] == "喂喂喂，这里是119。"
    assert event["startTimeMs"] == 100
    assert event["endTimeMs"] == 1500
    assert event["durationMs"] == 1400
    assert isinstance(event["sendTimeMs"], int)
    assert kwargs == {"publish_to_rabbitmq": False}


def test_provider_result_labels_monitor_and_client_speech_final_with_actual_provider(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    client = FakeClientWs()
    session = BridgeSession(
        client,
        FakeUpstreamWs([]),
        "call-provider-label",
        callfrom="8001",
        callto="8002",
    )
    session.speaker = "caller"
    session.direction = "inbound"
    session.current_segment_id = "caller-0001"
    session._valid_segment_ids.add("caller-0001")

    asyncio.run(session._handle_provider_result(asr_bridge.ProviderResult(
        provider="xfyun",
        segment_id="caller-0001",
        text="贵阳这里着火了",
        is_final=True,
        mode="xfyun-offline",
    )))

    monitor_event, kwargs = events[0]
    assert monitor_event["event"] == "speech.final"
    assert monitor_event["provider"] == "xfyun"
    assert kwargs == {"publish_to_rabbitmq": False}
    assert client.sent[-1]["eventType"] == "speech.final"
    assert client.sent[-1]["payload"]["provider"] == "xfyun"


def test_audio_frame_marks_new_funasr_segment_valid_when_vad_starts(monkeypatch):
    monkeypatch.setattr(
        asr_bridge.AudioPreprocessor,
        "process",
        lambda self, call_id, speaker, pcm, **kwargs: type(
            "AudioResult",
            (),
            {
                "pcm": pcm,
                "diagnostics": [],
                "raw_db": -20.0,
                "processed_db": -20.0,
                "gain_db": 0.0,
                "audio_level": 80,
            },
        )(),
    )

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-valid")
    session._vad = FakeVAD([FakeVADResult(speech_started=True, is_speaking=True)])

    event = {
        "eventType": "audio.frame",
        "callId": "call-valid",
        "seq": 1,
        "payload": {
            "audioBase64": base64.b64encode(b"\x01\x00" * 320).decode("ascii"),
            "callfrom": "micro",
            "callto": "micro",
            "speaker": "micro",
            "direction": "caller",
            "startTimeMs": 0,
            "endTimeMs": 20,
        },
    }

    asyncio.run(session._on_audio_frame(event))

    assert session.current_segment_id == "micro-0001"
    assert "micro-0001" in session._valid_segment_ids


def test_offline_text_is_published_to_rabbitmq_only_when_turn_completes(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    upstream = FakeUpstreamWs([
        FakeMessage(json.dumps({
            "mode": "2pass-offline",
            "text": "科兴软件园发生火灾。",
            "is_final": True,
            "wav_name": "call-2__caller-0001",
        }, ensure_ascii=False))
    ])
    client = FakeClientWs()
    session = BridgeSession(client, upstream, "call-2", callfrom="8001", callto="8002")
    session.speaker = "caller"
    session.direction = "inbound"
    session._valid_segment_ids.add("caller-0001")

    asyncio.run(session._upstream_to_client())

    assert len(events) == 1
    event, kwargs = events[0]
    assert event["event"] == "speech.final"
    assert event["text"] == "科兴软件园发生火灾。"
    assert kwargs == {"publish_to_rabbitmq": False}
    assert client.sent[0]["eventType"] == "speech.final"

    completed = CompletedTurnRecording(
        call_id="call-2",
        callfrom="8001",
        callto="8002",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=1200,
    )
    asyncio.run(session._publish_completed_turn_text(completed))

    final_event, final_kwargs = events[-1]
    assert final_event["event"] == "speech.final"
    assert final_event["text"] == "科兴软件园发生火灾。"
    assert final_event["segmentIds"] == ["caller-0001"]
    assert final_event["finalSource"] == "offline"
    assert final_event["provider"] == "funasr"
    assert final_event["providers"] == ["funasr"]
    assert final_kwargs == {"publish_to_rabbitmq": True}


def test_completed_turn_marks_cross_provider_segments_as_mixed(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(
        FakeClientWs(),
        FakeUpstreamWs([]),
        "call-mixed-provider",
        callfrom="8001",
        callto="8002",
    )
    session.speaker = "caller"
    session.direction = "inbound"
    session._valid_segment_ids.update({"caller-0001", "caller-0002"})

    async def scenario():
        await session._handle_provider_result(asr_bridge.ProviderResult(
            provider="funasr",
            segment_id="caller-0001",
            text="我在贵阳",
            is_final=True,
            mode="2pass-offline",
        ))
        await session._handle_provider_result(asr_bridge.ProviderResult(
            provider="xfyun",
            segment_id="caller-0002",
            text="这里着火了",
            is_final=True,
            mode="xfyun-offline",
        ))
        completed = CompletedTurnRecording(
            call_id="call-mixed-provider",
            callfrom="8001",
            callto="8002",
            speaker="caller",
            direction="inbound",
            segment_ids=("caller-0001", "caller-0002"),
            pcm=b"\x01\x00" * 1600,
            start_time_ms=100,
            end_time_ms=1800,
        )
        await session._publish_completed_turn_text(completed)

    asyncio.run(scenario())

    final_event, final_kwargs = events[-1]
    assert final_event["event"] == "speech.final"
    assert final_event["segmentIds"] == ["caller-0001", "caller-0002"]
    assert final_event["provider"] == "mixed"
    assert final_event["providers"] == ["funasr", "xfyun"]
    assert final_kwargs == {"publish_to_rabbitmq": True}


def test_monitor_broadcast_also_publishes_to_rabbitmq(monkeypatch):
    published = []
    sent = []

    class FakePublisher:
        def publish(self, event):
            published.append(event)
            return {
                "exchange": "ids:asr",
                "routingKey": "asr.119",
                "payload": {"id": "evt-1", "type": "ids:asr:speech.final", "data": event},
            }

    class FakeMonitorWs:
        closed = False

        async def send_str(self, payload):
            sent.append(json.loads(payload))

    fake_publisher = FakePublisher()
    monkeypatch.setattr(asr_bridge, "_rabbitmq_publisher", fake_publisher, raising=False)
    monkeypatch.setattr(asr_bridge, "_RABBITMQ_PUBLISHERS", [fake_publisher], raising=False)
    asr_bridge._monitor_sockets.clear()
    asr_bridge._monitor_sockets.add(FakeMonitorWs())
    event = {"event": "speech.final", "callId": "call-1", "callto": "119", "text": "着火了"}

    asyncio.run(asr_bridge._broadcast_to_monitors(event))

    assert published == [event]
    assert sent[0] == {
        "event": "rabbitmq.message",
        "exchange": "ids:asr",
        "routingKey": "asr.119",
        "payload": {"id": "evt-1", "type": "ids:asr:speech.final", "data": event},
    }
    assert sent[1] == event
    asr_bridge._monitor_sockets.clear()


def test_stable_event_goes_to_message_service_and_qs(monkeypatch):
    message_events = []
    qs_events = []

    class FakeMessageConfig:
        enabled = True

    class FakeMessagePublisher:
        config = FakeMessageConfig()

        def enqueue(self, event):
            message_events.append(event)
            return True

    class FakeQsPublisher:
        exchange = "ids:qs"

        def publish(self, event):
            qs_events.append(event)
            return {
                "exchange": "ids:qs",
                "routingKey": "qs",
                "payload": {"type": "ids:qs:speech.final", "data": event},
            }

    monkeypatch.setattr(
        asr_bridge, "_message_service_publisher", FakeMessagePublisher(), raising=False
    )
    monkeypatch.setattr(
        asr_bridge, "_RABBITMQ_PUBLISHERS", [FakeQsPublisher()], raising=False
    )
    asr_bridge._monitor_sockets.clear()
    event = {
        "event": "speech.final",
        "callId": "call-new-channel",
        "callto": "119",
        "text": "科兴科学园发生火灾",
    }

    asyncio.run(asr_bridge._broadcast_to_monitors(event))

    assert message_events == [event]
    assert qs_events == [event]


def test_monitor_broadcast_can_skip_rabbitmq(monkeypatch):
    published = []
    sent = []

    class FakePublisher:
        def publish(self, event):
            published.append(event)
            return {
                "exchange": "ids:asr",
                "routingKey": "asr.119",
                "payload": {"id": "evt-1", "type": "ids:asr:speech.final", "data": event},
            }

    class FakeMonitorWs:
        closed = False

        async def send_str(self, payload):
            sent.append(json.loads(payload))

    fake_publisher = FakePublisher()
    monkeypatch.setattr(asr_bridge, "_rabbitmq_publisher", fake_publisher, raising=False)
    monkeypatch.setattr(asr_bridge, "_RABBITMQ_PUBLISHERS", [fake_publisher], raising=False)
    asr_bridge._monitor_sockets.clear()
    asr_bridge._monitor_sockets.add(FakeMonitorWs())
    event = {"event": "speech.vad", "callId": "call-1", "callto": "119", "vadState": "speaking"}

    asyncio.run(asr_bridge._broadcast_to_monitors(event, publish_to_rabbitmq=False))

    assert published == []
    assert sent == [event]
    asr_bridge._monitor_sockets.clear()


def test_broadcast_call_history_pushes_records_to_rabbitmq(monkeypatch):
    published = []

    class FakePublisher:
        exchange = "ids:asr"

        def publish(self, event):
            published.append(event)
            return {
                "exchange": "ids:asr",
                "routingKey": f"asr.{event.get('callto') or 'unknown'}",
                "payload": {"id": "evt-history", "type": "ids:asr:call.history", "data": event},
            }

    monkeypatch.setattr(asr_bridge, "_RABBITMQ_PUBLISHERS", [FakePublisher()], raising=False)
    sent = []

    class FakeMonitorWs:
        closed = False

        async def send_str(self, payload):
            sent.append(json.loads(payload))

    asr_bridge._monitor_sockets.clear()
    asr_bridge._monitor_sockets.add(FakeMonitorWs())

    records = [{
        "callId": "call-1",
        "segmentId": "caller-0001",
        "speaker": "caller",
        "text": "这里着火了",
    }]

    event = asyncio.run(asr_bridge.broadcast_call_history("call-1", records, callto="8016"))

    assert event["event"] == "call.history"
    assert event["callId"] == "call-1"
    assert event["callto"] == "8016"
    assert event["records"] == records
    assert event["count"] == 1
    assert event["monitorCount"] == 1
    assert isinstance(event["sendTimeMs"], int)
    assert published == [event]
    assert sent[0]["event"] == "rabbitmq.message"
    assert sent[0]["routingKey"] == "asr.8016"
    assert sent[1] == event
    asr_bridge._monitor_sockets.clear()


def test_monitor_broadcast_skips_unlisted_rabbitmq_events_by_default(monkeypatch):
    published = []
    sent = []

    class FakePublisher:
        def publish(self, event):
            published.append(event)
            return {
                "exchange": "ids:asr",
                "routingKey": "asr.119",
                "payload": {"id": "evt-1", "type": "ids:asr:call.started", "data": event},
            }

    class FakeMonitorWs:
        closed = False

        async def send_str(self, payload):
            sent.append(json.loads(payload))

    fake_publisher = FakePublisher()
    monkeypatch.setattr(asr_bridge, "_rabbitmq_publisher", fake_publisher, raising=False)
    monkeypatch.setattr(asr_bridge, "_RABBITMQ_PUBLISHERS", [fake_publisher], raising=False)
    asr_bridge._monitor_sockets.clear()
    asr_bridge._monitor_sockets.add(FakeMonitorWs())
    event = {"event": "call.started", "callId": "call-1", "callto": "119"}

    asyncio.run(asr_bridge._broadcast_to_monitors(event))

    assert published == []
    assert sent == [event]
    asr_bridge._monitor_sockets.clear()


class FakeVADResult:
    def __init__(self, is_speaking=False, speech_started=False, speech_ended=False, silence_duration_ms=0, audio_segment=None):
        self.is_speaking = is_speaking
        self.speech_started = speech_started
        self.speech_ended = speech_ended
        self.silence_duration_ms = silence_duration_ms
        self.audio_segment = audio_segment


class FakeVAD:
    def __init__(self, results):
        self.results = list(results)
        self.fed = []

    def feed(self, pcm):
        self.fed.append(pcm)
        if self.results:
            return self.results.pop(0)
        return FakeVADResult()

    def flush(self):
        return b""


class FakeAckClientWs(FakeClientWs):
    pass


class ClosingClientWs(FakeClientWs):
    async def send_str(self, payload: str):
        raise ClientConnectionResetError("closing")


class FakeSendUpstreamWs:
    def __init__(self):
        self.sent_bytes = []
        self.sent_text = []

    async def send_bytes(self, payload: bytes):
        self.sent_bytes.append(payload)

    async def send_str(self, payload: str):
        self.sent_text.append(json.loads(payload))


class FakeSegmentProvider:
    def __init__(self, name, segment_id):
        self.name = name
        self.segment_id = segment_id
        self.started = False
        self.finished = False
        self.sent_audio = []

    async def start(self):
        self.started = True

    async def send_audio(self, pcm):
        self.sent_audio.append(pcm)

    async def finish(self):
        self.finished = True

    async def close(self):
        self.finished = True

    async def events(self):
        if False:
            yield None


class FakeProviderFactory:
    def __init__(self):
        self.created = []

    def availability(self, name):
        return (name in {"funasr", "xfyun"}, "")

    async def create(self, name, segment_id, *, hotwords=""):
        provider = FakeSegmentProvider(name, segment_id)
        await provider.start()
        self.created.append(provider)
        return provider


class DelayedSwitchProviderFactory(FakeProviderFactory):
    def __init__(self, *, fail_target=False):
        super().__init__()
        self.fail_target = fail_target
        self.target_started = asyncio.Event()
        self.release_target = asyncio.Event()

    async def create(self, name, segment_id, *, hotwords=""):
        if name == "xfyun":
            self.target_started.set()
            await self.release_target.wait()
            if self.fail_target:
                raise asr_bridge.ProviderError(
                    "XFYUN_CONNECT_FAILED",
                    "科大讯飞连接失败",
                    provider="xfyun",
                )
        return await super().create(name, segment_id, hotwords=hotwords)


def write_hotword_fixture(root: Path):
    (root / "scenes").mkdir(parents=True)
    (root / "address.txt").write_text("科兴科学园\n科苑北路\n", encoding="utf-8")
    (root / "inquiry_fire_base.txt").write_text("着火\n冒烟\n", encoding="utf-8")
    (root / "scenes" / "highrise.txt").write_text("高层建筑\n超高层\n", encoding="utf-8")
    (root / "scenes" / "crowded_place.txt").write_text("商场\n学校\n", encoding="utf-8")
    (root / "scenes" / "chemical.txt").write_text("液化气\n危化品\n", encoding="utf-8")
    (root / "scenes" / "elevator.txt").write_text("电梯困人\n卡住\n", encoding="utf-8")


def max_abs_sample(pcm: bytes) -> int:
    values = array("h")
    values.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return max(abs(v) for v in values) if values else 0


def make_audio_event(seq=1, speaker="caller", callfrom="8001", callto="8002", pcm=None, start_ms=0, end_ms=30):
    import base64
    pcm = pcm if pcm is not None else b"\0" * 960
    return {
        "seq": seq,
        "payload": {
            "audioBase64": base64.b64encode(pcm).decode(),
            "speaker": speaker,
            "direction": "inbound",
            "callfrom": callfrom,
            "callto": callto,
            "startTimeMs": start_ms,
            "endTimeMs": end_ms,
        },
    }, pcm


def test_audio_frame_is_not_sent_to_funasr_until_vad_confirms_speech():
    event, _ = make_audio_event(seq=1, pcm=b"\x20\x00" * 1600, start_ms=0, end_ms=100)
    client = FakeAckClientWs()
    upstream = FakeSendUpstreamWs()
    session = BridgeSession(client, upstream, "call-noise", callfrom="8001", callto="119")
    session._vad = FakeVAD([FakeVADResult(is_speaking=False)])

    asyncio.run(session._on_audio_frame(event))

    assert upstream.sent_text == []
    assert upstream.sent_bytes == []
    assert client.sent[-1]["accepted"] is True


def test_immediate_switch_cuts_old_provider_and_replays_buffer_without_waiting_for_next_vad(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = DelayedSwitchProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-immediate",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([FakeVADResult(is_speaking=True)])
    event, pcm = make_audio_event(seq=1, pcm=b"\x20\x00" * 640)

    async def scenario():
        old_provider = await factory.create("funasr", "caller-0001")
        session._active_provider = old_provider
        session.current_segment_id = "caller-0001"
        session.current_provider = "funasr"
        session.handshake_sent = True

        result = await session.request_model_switch("xfyun", "req-immediate")
        await asyncio.wait_for(factory.target_started.wait(), timeout=0.2)
        await session._on_audio_frame(event)
        assert old_provider.sent_audio == []

        factory.release_target.set()
        await asyncio.wait_for(session._switch_task, timeout=0.2)
        return result, old_provider

    result, old_provider = asyncio.run(scenario())

    assert result["accepted"] is True
    assert old_provider.finished is True
    assert session.current_provider == "xfyun"
    assert session.pending_provider is None
    assert factory.created[-1].name == "xfyun"
    assert factory.created[-1].sent_audio == [pcm]
    assert [
        item["event"] for item in events
        if item["event"].startswith("asr.model")
    ] == ["asr.model.switch.pending", "asr.model.changed"]


def test_immediate_switch_always_broadcasts_pending_before_changed(monkeypatch):
    events = []

    async def yielding_broadcast(event, **kwargs):
        if event["event"] == "asr.model.switch.pending":
            await asyncio.sleep(0)
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", yielding_broadcast)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-event-order",
        provider_factory=FakeProviderFactory(),
    )

    async def scenario():
        await session.request_model_switch("xfyun", "req-order")
        await session._switch_task

    asyncio.run(scenario())

    assert [item["event"] for item in events] == [
        "asr.model.switch.pending",
        "asr.model.changed",
    ]


def test_requesting_current_provider_is_idempotent_state_sync(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-same-provider",
        provider_factory=FakeProviderFactory(),
    )

    result = asyncio.run(session.request_model_switch("funasr", "req-same-provider"))

    assert result["accepted"] is True
    assert result["changed"] is False
    assert [event["event"] for event in events] == ["asr.model.state"]
    assert events[0]["requestId"] == "req-same-provider"
    assert events[0]["currentProvider"] == "funasr"


def test_immediate_switch_finishes_target_when_speech_ends_during_connect(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = DelayedSwitchProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-switch-speech-end",
        provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([
        FakeVADResult(is_speaking=False, speech_ended=True, audio_segment=b"segment"),
    ])
    event, pcm = make_audio_event(seq=1, pcm=b"\x20\x00" * 640)

    async def scenario():
        await session.request_model_switch("xfyun", "req-speech-end")
        await asyncio.wait_for(factory.target_started.wait(), timeout=0.2)
        await session._on_audio_frame(event)
        factory.release_target.set()
        await asyncio.wait_for(session._switch_task, timeout=0.2)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    target = factory.created[-1]
    assert target.name == "xfyun"
    assert target.sent_audio == [pcm]
    assert target.finished is True
    assert session._active_provider is None


def test_immediate_switch_timeout_falls_back_with_stable_error_code(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    class TimeoutFactory(FakeProviderFactory):
        async def create(self, name, segment_id, *, hotwords=""):
            if name == "xfyun":
                await asyncio.sleep(1)
            return await super().create(name, segment_id, hotwords=hotwords)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-switch-timeout",
        provider_factory=TimeoutFactory(),
    )
    session._switch_timeout_seconds = 0.01

    async def scenario():
        await session.request_model_switch("xfyun", "req-timeout")
        await asyncio.wait_for(session._switch_task, timeout=0.2)

    asyncio.run(scenario())

    failure = next(item for item in events if item["event"] == "asr.model.switch.failed")
    assert failure["errorCode"] == "MODEL_SWITCH_TIMEOUT"
    assert session.current_provider == "funasr"


def test_immediate_switch_buffer_limit_cancels_target_and_replays_to_funasr(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = DelayedSwitchProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-switch-buffer-limit",
        provider_factory=factory,
    )
    session._switch_buffer_max_bytes = 16
    session._vad = FakeVAD([FakeVADResult(is_speaking=True)])
    event, pcm = make_audio_event(seq=1, pcm=b"\x20\x00" * 16)

    async def scenario():
        await session.request_model_switch("xfyun", "req-buffer-limit")
        await asyncio.wait_for(factory.target_started.wait(), timeout=0.2)
        await session._on_audio_frame(event)

    asyncio.run(scenario())

    failure = next(item for item in events if item["event"] == "asr.model.switch.failed")
    assert failure["errorCode"] == "MODEL_SWITCH_BUFFER_LIMIT"
    assert factory.created[-1].name == "funasr"
    assert factory.created[-1].sent_audio == [pcm]
    assert session.current_provider == "funasr"


def test_immediate_switch_failure_replays_buffer_to_funasr(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = DelayedSwitchProviderFactory(fail_target=True)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-immediate-fallback",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([FakeVADResult(is_speaking=True)])
    event, pcm = make_audio_event(seq=1, pcm=b"\x30\x00" * 640)

    async def scenario():
        await session.request_model_switch("xfyun", "req-immediate-fail")
        await asyncio.wait_for(factory.target_started.wait(), timeout=0.2)
        await session._on_audio_frame(event)
        factory.release_target.set()
        await asyncio.wait_for(session._switch_task, timeout=0.2)

    asyncio.run(scenario())

    assert session.current_provider == "funasr"
    assert session.pending_provider is None
    assert factory.created[-1].name == "funasr"
    assert factory.created[-1].sent_audio == [pcm]
    assert any(item["event"] == "asr.model.switch.failed" for item in events)


def test_switch_active_sessions_starts_both_connections_concurrently():
    both_started = asyncio.Event()
    started = []

    class BlockingSession:
        call_ended = False

        def __init__(self, call_id):
            self.call_id = call_id

        async def request_model_switch(self, target_provider, request_id):
            started.append(self.call_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            return {"accepted": True}

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions.update({
            "caller-stream": BlockingSession("caller-stream"),
            "agent-stream": BlockingSession("agent-stream"),
        })
        try:
            return await asr_bridge.switch_active_session_models(
                ["caller-stream", "agent-stream"],
                "xfyun",
                "req-parallel",
            )
        finally:
            asr_bridge._active_sessions.clear()

    result = asyncio.run(scenario())

    assert started == ["caller-stream", "agent-stream"]
    assert result["acceptedCallIds"] == ["caller-stream", "agent-stream"]


def test_immediate_model_switch_routes_current_vad_audio_to_target(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FakeProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-switch",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([FakeVADResult(speech_started=True, is_speaking=True)])
    event, pcm = make_audio_event(seq=1, pcm=b"\x20\x00" * 640)

    async def scenario():
        result = await session.request_model_switch("xfyun", "req-1")
        await session._on_audio_frame(event)
        return result

    result = asyncio.run(scenario())

    assert result["accepted"] is True
    assert session.current_provider == "xfyun"
    assert session.pending_provider is None
    assert factory.created[0].name == "xfyun"
    assert factory.created[0].sent_audio == [pcm]
    assert [item["event"] for item in events if item["event"].startswith("asr.model")] == [
        "asr.model.switch.pending",
        "asr.model.changed",
    ]


def test_switch_requested_during_speech_buffers_frames_until_target_connects(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FakeProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-boundary",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([
        FakeVADResult(speech_started=True, is_speaking=True),
        FakeVADResult(is_speaking=True),
        FakeVADResult(speech_ended=True, audio_segment=b"first-segment"),
        FakeVADResult(speech_started=True, is_speaking=True),
    ])

    async def scenario():
        await session._on_audio_frame(make_audio_event(seq=1)[0])
        await session.request_model_switch("xfyun", "req-2")
        await session._on_audio_frame(make_audio_event(seq=2)[0])
        assert session.current_provider == "funasr"
        assert session.pending_provider == "xfyun"
        await session._on_audio_frame(make_audio_event(seq=3)[0])
        await session._on_audio_frame(make_audio_event(seq=4)[0])

    asyncio.run(scenario())

    assert [provider.name for provider in factory.created] == ["funasr", "xfyun"]
    assert factory.created[0].finished is True
    assert session.current_provider == "xfyun"


def test_immediate_model_switch_can_return_from_xfyun_to_funasr(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FakeProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-switch-back",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.current_provider = "xfyun"
    session.speaker = "caller"
    session._vad = FakeVAD([FakeVADResult(speech_started=True, is_speaking=True)])

    async def scenario():
        result = await session.request_model_switch("funasr", "req-back")
        await session._on_audio_frame(make_audio_event(seq=1)[0])
        return result

    result = asyncio.run(scenario())

    assert result["accepted"] is True
    assert session.current_provider == "funasr"
    assert session.pending_provider is None
    assert [provider.name for provider in factory.created] == ["funasr"]
    assert [
        event["event"] for event in events
        if event["event"].startswith("asr.model")
    ] == [
        "asr.model.switch.pending",
        "asr.model.changed",
    ]


def test_switch_active_sessions_applies_same_request_to_both_streams(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    caller = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "caller-stream",
        callfrom="8001", callto="8002", provider_factory=FakeProviderFactory(),
    )
    agent = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "agent-stream",
        callfrom="8001", callto="8002", provider_factory=FakeProviderFactory(),
    )

    async def scenario():
        asr_bridge._active_sessions.clear()
        await asr_bridge.register_session(caller)
        await asr_bridge.register_session(agent)
        result = await asr_bridge.switch_active_session_models(
            ["caller-stream", "agent-stream", "ended-stream"],
            "xfyun",
            "req-both",
        )
        await asyncio.gather(caller._switch_task, agent._switch_task)
        asr_bridge._active_sessions.clear()
        return result

    result = asyncio.run(scenario())

    assert result["acceptedCallIds"] == ["caller-stream", "agent-stream"]
    assert result["missingCallIds"] == ["ended-stream"]
    assert caller.current_provider == "xfyun"
    assert agent.current_provider == "xfyun"
    assert caller.pending_provider is None
    assert agent.pending_provider is None


def test_reconnected_call_inherits_model_and_old_session_cannot_unregister_it():
    first = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "reconnected-call",
        provider_factory=FakeProviderFactory(),
    )

    async def scenario():
        await asr_bridge.register_session(first)
        asr_bridge._remember_model_preference(first.call_id, "xfyun")
        replacement = BridgeSession(
            FakeAckClientWs(), FakeSendUpstreamWs(), first.call_id,
            provider_factory=FakeProviderFactory(),
        )
        await asr_bridge.register_session(replacement)
        old_removed = await asr_bridge.unregister_session(first.call_id, first)
        states = await asr_bridge.active_session_model_states()
        asr_bridge._active_sessions.clear()
        return replacement, states, old_removed

    replacement, states, old_removed = asyncio.run(scenario())

    assert old_removed is False
    assert first.call_ended is True
    assert replacement.current_provider == "xfyun"
    assert [state["callId"] for state in states] == ["reconnected-call"]
    assert states[0]["currentProvider"] == "xfyun"


def test_xfyun_start_failure_falls_back_to_funasr_without_dropping_call(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    class FailingXfyunFactory(FakeProviderFactory):
        async def create(self, name, segment_id, *, hotwords=""):
            if name == "xfyun":
                raise asr_bridge.ProviderError(
                    "XFYUN_AUTH_FAILED",
                    "科大讯飞连接失败",
                    provider="xfyun",
                )
            return await super().create(name, segment_id, hotwords=hotwords)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FailingXfyunFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-fallback",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.speaker = "caller"
    session._vad = FakeVAD([FakeVADResult(speech_started=True, is_speaking=True)])

    async def scenario():
        await session.request_model_switch("xfyun", "req-fallback")
        await session._on_audio_frame(make_audio_event(seq=1)[0])
        await session._switch_task

    asyncio.run(scenario())

    assert session.current_provider == "funasr"
    assert session.pending_provider is None
    assert [provider.name for provider in factory.created] == ["funasr"]
    assert any(event["event"] == "asr.model.switch.failed" for event in events)


def test_xfyun_midsegment_failure_replays_with_funasr_then_retries_xfyun(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FakeProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-replay",
        callfrom="8001", callto="8002", provider_factory=factory,
    )
    session.current_provider = "xfyun"
    full_audio = b"complete-vad-audio"

    async def scenario():
        provider = await factory.create("xfyun", "caller-0001")
        session._providers.add(provider)
        session._segment_audio_cache["caller-0001"] = full_audio
        await session._handle_provider_failure(provider, asr_bridge.ProviderResult(
            provider="xfyun",
            segment_id="caller-0001",
            error_code="XFYUN_CONNECTIONERROR",
            error_message="科大讯飞连接异常",
        ))
        return await session._start_provider_segment(segment_id="caller-0002")

    next_provider = asyncio.run(scenario())

    assert [provider.name for provider in factory.created] == ["xfyun", "funasr", "xfyun"]
    assert factory.created[1].sent_audio == [full_audio]
    assert factory.created[1].finished is True
    assert next_provider.name == "xfyun"
    assert session.current_provider == "xfyun"
    assert not any(event["event"] == "asr.model.switch.failed" for event in events)


def test_established_xfyun_start_failure_uses_funasr_for_one_segment_only(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    class FailFirstXfyunFactory(FakeProviderFactory):
        def __init__(self):
            super().__init__()
            self.xfyun_attempts = 0

        async def create(self, name, segment_id, *, hotwords=""):
            if name == "xfyun":
                self.xfyun_attempts += 1
                if self.xfyun_attempts == 1:
                    raise asr_bridge.ProviderError(
                        "XFYUN_CONNECT_FAILED",
                        "科大讯飞单段连接失败",
                        provider="xfyun",
                    )
            return await super().create(name, segment_id, hotwords=hotwords)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FailFirstXfyunFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-start-retry",
        provider_factory=factory,
    )
    session.current_provider = "xfyun"

    async def scenario():
        fallback = await session._start_provider_segment(segment_id="caller-0001")
        session._active_provider = None
        retry = await session._start_provider_segment(segment_id="caller-0002")
        return fallback, retry

    fallback, retry = asyncio.run(scenario())

    assert fallback.name == "funasr"
    assert retry.name == "xfyun"
    assert session.current_provider == "xfyun"
    assert not any(event["event"] == "asr.model.switch.failed" for event in events)


def test_provider_failure_after_call_ended_is_ignored(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = FakeProviderFactory()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-ended-provider-failure",
        provider_factory=factory,
    )
    session.current_provider = "xfyun"
    session.call_ended = True

    async def scenario():
        provider = await factory.create("xfyun", "caller-0001")
        await session._handle_provider_failure(provider, asr_bridge.ProviderResult(
            provider="xfyun",
            segment_id="caller-0001",
            error_code="XFYUN_CONNECTIONERROR",
            error_message="科大讯飞连接已关闭",
        ))

    asyncio.run(scenario())

    assert session.current_provider == "xfyun"
    assert session.pending_provider is None
    assert events == []
    assert session._failed_provider_segments == set()


def test_call_ended_closes_state_before_finishing_provider(monkeypatch):
    events = []
    ended_state_seen_by_finish = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-end-order",
        provider_factory=FakeProviderFactory(),
    )
    session.current_provider = "xfyun"
    session.speaker = "caller"
    session.current_segment_id = "caller-0001"

    class FailureOnFinishProvider(FakeSegmentProvider):
        async def finish(self):
            ended_state_seen_by_finish.append(session.call_ended)
            await session._handle_provider_failure(self, asr_bridge.ProviderResult(
                provider="xfyun",
                segment_id=self.segment_id,
                error_code="XFYUN_CONNECTIONERROR",
                error_message="科大讯飞连接已关闭",
            ))
            self.finished = True

    provider = FailureOnFinishProvider("xfyun", "caller-0001")
    session._active_provider = provider
    session.handshake_sent = True

    asyncio.run(session._on_call_ended({"seq": 9}))

    assert ended_state_seen_by_finish == [True]
    assert session.call_ended is True
    assert session.current_provider == "xfyun"
    assert not any(event["event"] == "asr.model.switch.failed" for event in events)
    assert any(event["event"] == "call.ended" for event in events)


def test_call_ended_during_target_connect_does_not_broadcast_switch_failure(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    factory = DelayedSwitchProviderFactory(fail_target=True)
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-end-during-connect",
        provider_factory=factory,
    )

    async def scenario():
        await session.request_model_switch("xfyun", "req-end-during-connect")
        await asyncio.wait_for(factory.target_started.wait(), timeout=0.2)
        end_task = asyncio.create_task(session._on_call_ended({"seq": 10}))
        await asyncio.sleep(0)
        assert session.call_ended is True
        factory.release_target.set()
        await asyncio.wait_for(end_task, timeout=0.2)

    asyncio.run(scenario())

    assert session.call_ended is True
    assert not any(event["event"] == "asr.model.switch.failed" for event in events)
    assert any(event["event"] == "call.ended" for event in events)


def test_held_call_id_audio_frame_is_acknowledged_but_not_processed(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    hold_state = CallHoldState()
    hold_state.apply_cti_event({
        "eventId": "evt-hold",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })
    monkeypatch.setattr(asr_bridge, "_call_hold_state", hold_state)
    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    event, _ = make_audio_event(seq=7, speaker="caller", callfrom="8015", callto="8014", pcm=b"\x20\x00" * 1600)
    client = FakeAckClientWs()
    upstream = FakeSendUpstreamWs()
    session = BridgeSession(client, upstream, "caller-call", callfrom="8015", callto="8014")
    session._vad = FakeVAD([FakeVADResult(is_speaking=True, speech_started=True)])

    asyncio.run(session._on_audio_frame(event))

    assert client.sent[-1]["accepted"] is True
    assert client.sent[-1]["receivedSeq"] == 7
    assert upstream.sent_text == []
    assert upstream.sent_bytes == []
    assert session._vad.fed == []
    assert events == []


def test_held_phone_pair_blocks_peer_agent_stream(monkeypatch):
    hold_state = CallHoldState()
    hold_state.apply_cti_event({
        "eventId": "evt-hold",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })
    monkeypatch.setattr(asr_bridge, "_call_hold_state", hold_state)

    event, _ = make_audio_event(seq=8, speaker="agent", callfrom="8015", callto="8014", pcm=b"\x20\x00" * 1600)
    client = FakeAckClientWs()
    upstream = FakeSendUpstreamWs()
    session = BridgeSession(client, upstream, "agent-call", callfrom="8015", callto="8014")
    session._vad = FakeVAD([FakeVADResult(is_speaking=True, speech_started=True)])

    asyncio.run(session._on_audio_frame(event))

    assert client.sent[-1]["accepted"] is True
    assert upstream.sent_text == []
    assert upstream.sent_bytes == []
    assert session._vad.fed == []


def test_held_call_skips_speech_broadcast_and_database_remember(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    class CapturingDatabase:
        def __init__(self):
            self.events = []

        def remember_speech(self, event):
            self.events.append(dict(event))

    hold_state = CallHoldState()
    hold_state.apply_cti_event({
        "eventId": "evt-hold",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })
    monkeypatch.setattr(asr_bridge, "_call_hold_state", hold_state)
    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    db = CapturingDatabase()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "caller-call",
        callfrom="8015", callto="8014", database_writer=db,
    )
    session.speaker = "caller"

    asyncio.run(session._broadcast_speech_to_monitors("保持期间内部沟通", segment_id="caller-0001"))

    assert events == []
    assert db.events == []


def test_held_phone_pair_skips_completed_turn_text(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    hold_state = CallHoldState()
    hold_state.apply_cti_event({
        "eventId": "evt-hold",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })
    monkeypatch.setattr(asr_bridge, "_call_hold_state", hold_state)
    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "agent-call", callfrom="8015", callto="8014")
    completed = CompletedTurnRecording(
        call_id="agent-call",
        callfrom="8015",
        callto="8014",
        speaker="agent",
        direction="outbound",
        segment_ids=("agent-0001",),
        pcm=b"internal-audio",
        start_time_ms=0,
        end_time_ms=1000,
    )

    published = asyncio.run(session._publish_completed_turn_text(completed))

    assert published is True
    assert events == []


def test_held_phone_pair_skips_completed_audio_save(monkeypatch):
    events = []

    async def fake_broadcast(event):
        events.append(event)

    class CapturingDatabase:
        def __init__(self):
            self.audio_events = []

        def save_audio_turn(self, event):
            self.audio_events.append(dict(event))

    hold_state = CallHoldState()
    hold_state.apply_cti_event({
        "eventId": "evt-hold",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })
    monkeypatch.setattr(asr_bridge, "_call_hold_state", hold_state)
    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = FakeRecordingStore()
    db = CapturingDatabase()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "agent-call",
        callfrom="8015", callto="8014", recording_store=recorder, database_writer=db,
    )
    completed = CompletedTurnRecording(
        call_id="agent-call",
        callfrom="8015",
        callto="8014",
        speaker="agent",
        direction="outbound",
        segment_ids=("agent-0001",),
        pcm=b"internal-audio",
        start_time_ms=0,
        end_time_ms=1000,
    )

    asyncio.run(session._save_and_broadcast_completed_audio(completed))

    assert recorder.saved == []
    assert db.audio_events == []
    assert events == []


def test_paraformer_chinese_character_spaces_are_normalized_in_streaming_event(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(FakeClientWs(), FakeSendUpstreamWs(), "call-spaces", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"

    asyncio.run(session._broadcast_speech_to_monitors("我 这 里 发 生 了 火 灾", segment_id="caller-0001"))

    event, _ = events[-1]
    assert event["text"] == "我这里发生了火灾"
    assert session._latest_speech_events["caller-0001"]["text"] == "我这里发生了火灾"
    assert session._transcript_turns()[0].text == "我这里发生了火灾"


@pytest.mark.parametrize("spoken, expected", [
    ("请拨打幺幺九", "请拨打119"),
    ("请拨打一一九", "请拨打119"),
    ("请拨打么么九", "请拨打119"),
    ("幺一九已经接通", "119已经接通"),
])
def test_fire_emergency_number_spoken_forms_are_normalized(spoken, expected):
    assert asr_bridge._normalize_asr_text(spoken) == expected


def test_funasr_upstream_text_spaces_are_normalized_for_client_and_monitor(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    upstream = FakeUpstreamWs([FakeMessage(json.dumps({
        "mode": "2pass-offline",
        "text": "请 问 具 体 地 址 在 哪 里",
        "is_final": True,
        "wav_name": "call-space-upstream__agent-0001",
    }, ensure_ascii=False))])
    client = FakeClientWs()
    session = BridgeSession(client, upstream, "call-space-upstream", callfrom="8001", callto="119")
    session.speaker = "agent"
    session.direction = "outbound"
    session._valid_segment_ids.add("agent-0001")

    asyncio.run(session._upstream_to_client())

    assert events[-1][0]["text"] == "请问具体地址在哪里"
    assert client.sent[0]["payload"]["text"] == "请问具体地址在哪里"


def test_speech_updates_latest_transcript_segment(monkeypatch):
    async def fake_broadcast(event, **kwargs):
        pass

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    session = BridgeSession(FakeClientWs(), FakeSendUpstreamWs(), "call-cache", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"
    session.turn_start_ms = 100
    session.turn_end_ms = 500

    asyncio.run(session._broadcast_speech_to_monitors("我这里发生了活", segment_id="caller-0001"))
    session.turn_end_ms = 900
    asyncio.run(session._broadcast_speech_to_monitors("我这里发生了火灾", segment_id="caller-0001"))

    turns = session._transcript_turns()
    assert len(turns) == 1
    assert turns[0].segment_id == "caller-0001"
    assert turns[0].text == "我这里发生了火灾"
    assert turns[0].start_time_ms == 100
    assert turns[0].end_time_ms == 900



def test_completed_turn_does_not_reuse_global_text_from_previous_call(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    old_session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "old-call", callfrom="8001", callto="8002")
    old_session.speaker = "agent"
    old_session.direction = "outbound"
    asyncio.run(old_session._broadcast_speech_to_monitors("上一通的问题", segment_id="agent-0001"))

    new_session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "new-call", callfrom="8001", callto="8002")
    completed = CompletedTurnRecording(
        call_id="new-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
        direction="outbound",
        segment_ids=("agent-0001",),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=900,
    )

    published = asyncio.run(new_session._publish_completed_turn_text(completed))

    assert published is False
    assert len(events) == 1
    assert events[0][0]["callId"] == "old-call"



def test_turn_completion_publishes_latest_streaming_text_as_rabbitmq_fallback(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-fallback", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"
    session.current_segment_id = "caller-0001"

    asyncio.run(session._broadcast_speech_to_monitors("我这里发生了火灾", segment_id="caller-0001"))
    assert events[-1][1] == {"publish_to_rabbitmq": False}

    completed = CompletedTurnRecording(
        call_id="call-fallback",
        callfrom="8001",
        callto="119",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=1200,
    )
    asyncio.run(session._publish_completed_turn_text(completed))

    fallback_event, fallback_kwargs = events[-1]
    assert fallback_event["event"] == "speech.final"
    assert fallback_event["text"] == "我这里发生了火灾"
    assert fallback_event["segmentIds"] == ["caller-0001"]
    assert fallback_event["finalSource"] == "turn-complete-streaming-fallback"
    assert fallback_kwargs == {"publish_to_rabbitmq": True}

def test_turn_completion_publishes_merged_turn_text_to_rabbitmq(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-merged", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"

    asyncio.run(session._broadcast_speech_to_monitors("喂你好", segment_id="caller-0001"))
    asyncio.run(session._broadcast_speech_to_monitors("我这里发生火灾", segment_id="caller-0002"))

    completed = CompletedTurnRecording(
        call_id="call-merged",
        callfrom="8001",
        callto="119",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001", "caller-0002"),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=2200,
    )
    asyncio.run(session._publish_completed_turn_text(completed))

    final_event, final_kwargs = events[-1]
    assert final_event["event"] == "speech.final"
    assert final_event["text"] == "喂你好我这里发生火灾"
    assert final_event["segmentIds"] == ["caller-0001", "caller-0002"]
    assert final_kwargs == {"publish_to_rabbitmq": True}


def test_turn_completion_dedupes_progressive_prefix_text(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-dedupe", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"

    asyncio.run(session._broadcast_speech_to_monitors("喂你好", segment_id="caller-0001"))
    asyncio.run(session._broadcast_speech_to_monitors("喂你好我是报警人", segment_id="caller-0002"))

    completed = CompletedTurnRecording(
        call_id="call-dedupe",
        callfrom="8001",
        callto="119",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001", "caller-0002"),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=2200,
    )
    asyncio.run(session._publish_completed_turn_text(completed))

    final_event, _ = events[-1]
    assert final_event["text"] == "喂你好我是报警人"


def test_turn_completion_dedupes_restarted_middle_phrase(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-middle-dedupe", callfrom="8001", callto="119")
    session.speaker = "caller"
    session.direction = "inbound"

    asyncio.run(session._broadcast_speech_to_monitors(
        "哎 哎 你 说 什 么我 这 边 困 了 三 十 多 个 人好 像 是 叫 深 圳 软 件 园 一 七",
        segment_id="caller-0001",
    ))
    asyncio.run(session._broadcast_speech_to_monitors(
        "我 这 边 困 了 三 十 多 个 人 好 像 是 叫 深 圳 软 件 园 一 七 七 栋",
        segment_id="caller-0002",
    ))

    completed = CompletedTurnRecording(
        call_id="call-middle-dedupe",
        callfrom="8001",
        callto="119",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001", "caller-0002"),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=2200,
    )
    asyncio.run(session._publish_completed_turn_text(completed))

    final_event, _ = events[-1]
    assert final_event["text"] == "哎哎你说什么我这边困了三十多个人好像是叫深圳软件园一七七栋"


def test_turn_completion_schedules_turn_correction_publish(monkeypatch):
    broadcast_events = []
    async def fake_broadcast(event, **kwargs):
        broadcast_events.append(event)

    class FakePostprocessor:
        def build_turn_event(self, **kwargs):
            assert kwargs["call_id"] == "call-ai"
            assert kwargs["callto"] == "119"
            assert kwargs["turn"].text == "我这里发生了火灾"
            return {
                "event": "call.corrected",
                "callId": "call-ai",
                "callto": "119",
                "correctionScope": "turn",
                "segmentId": kwargs["turn"].segment_id,
                "correctedText": "报警人：我这里发生了火灾",
                "turns": [{
                    "segmentId": kwargs["turn"].segment_id,
                    "speaker": "caller",
                    "correctedText": "我这里发生了火灾",
                    "keywords": ["火灾"],
                }],
            }

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    monkeypatch.setattr(asr_bridge.asyncio, "to_thread", immediate_to_thread)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-ai", callfrom="8001", callto="119")
    session._call_postprocessor = FakePostprocessor()
    session.speaker = "caller"
    session.direction = "inbound"
    session._latest_speech_events["caller-0001"] = {
        "event": "speech.final",
        "callId": "call-ai",
        "segmentId": "caller-0001",
        "callfrom": "8001",
        "callto": "119",
        "speaker": "caller",
        "direction": "inbound",
        "text": "我这里发生了火灾",
    }
    completed = CompletedTurnRecording(
        call_id="call-ai",
        callfrom="8001",
        callto="119",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"\x01\x00" * 1600,
        start_time_ms=100,
        end_time_ms=900,
    )

    async def scenario():
        await session._publish_completed_turn_text(completed)
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert [event["event"] for event in broadcast_events[-2:]] == ["speech.final", "call.corrected"]
    assert broadcast_events[-1]["correctionScope"] == "turn"
    assert broadcast_events[-1]["segmentId"] == "caller-0001"
    assert broadcast_events[-1]["segmentIds"] == ["caller-0001"]
    assert broadcast_events[-1]["turns"][0]["keywords"] == ["火灾"]


def test_turn_correction_is_forwarded_to_client(monkeypatch):
    async def fake_broadcast(event, **kwargs):
        return None

    class FakePostprocessor:
        def build_turn_event(self, **kwargs):
            return {
                "event": "call.corrected",
                "callId": "call-addressbot",
                "correctionScope": "turn",
                "segmentId": kwargs["turn"].segment_id,
                "correctedText": "我在科苑北路",
            }

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    monkeypatch.setattr(asr_bridge.asyncio, "to_thread", immediate_to_thread)

    client = FakeAckClientWs()
    session = BridgeSession(client, FakeSendUpstreamWs(), "call-addressbot", callfrom="micro", callto="micro")
    session._call_postprocessor = FakePostprocessor()
    event = {
        "event": "speech.final",
        "callId": "call-addressbot",
        "segmentId": "micro-0001",
        "callfrom": "micro",
        "callto": "micro",
        "speaker": "micro",
        "direction": "caller",
        "text": "我在科园北路",
    }

    asyncio.run(session._publish_turn_correction(event))

    assert client.sent[-1]["eventType"] == "call.corrected"
    assert client.sent[-1]["correctedText"] == "我在科苑北路"
    assert client.sent[-1]["segmentId"] == "micro-0001"


def test_call_ended_does_not_publish_extra_whole_call_correction(monkeypatch):
    broadcast_events = []

    async def fake_broadcast(event, **kwargs):
        broadcast_events.append(event)

    class FakePostprocessor:
        def build_event(self, **kwargs):
            raise AssertionError("call.ended should not trigger whole-call correction")

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(ClosingClientWs(), FakeSendUpstreamWs(), "caller-call", callfrom="8001", callto="119")
    session._call_postprocessor = FakePostprocessor()
    session._vad = FakeVAD([])

    asyncio.run(session._on_call_ended({"seq": 9}))

    assert [event["event"] for event in broadcast_events] == ["call.ended"]


def test_both_call_sides_publish_their_own_turn_correction_events(monkeypatch):
    broadcast_events = []

    async def fake_broadcast(event, **kwargs):
        broadcast_events.append(event)

    class FakePostprocessor:
        def build_turn_event(self, **kwargs):
            return {
                "event": "call.corrected",
                "callId": kwargs["call_id"],
                "callto": kwargs["callto"],
                "correctionScope": "turn",
                "segmentId": kwargs["turn"].segment_id,
                "turns": [
                    {
                        "segmentId": kwargs["turn"].segment_id,
                        "speaker": kwargs["turn"].speaker,
                        "keywords": ["火灾"],
                    }
                ],
            }

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    monkeypatch.setattr(asr_bridge.asyncio, "to_thread", immediate_to_thread)

    caller = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "caller-call", callfrom="8015", callto="8014")
    agent = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "agent-call", callfrom="8015", callto="8014")
    for session, speaker, segment_id, text in [
        (caller, "caller", "caller-0001", "我这里发生了火灾"),
        (agent, "agent", "agent-0001", "请问具体地址在哪里"),
    ]:
        session._call_postprocessor = FakePostprocessor()
        session.speaker = speaker
        session.direction = "inbound" if speaker == "caller" else "outbound"
        session._latest_speech_events[segment_id] = {
            "event": "speech.final",
            "callId": session.call_id,
            "segmentId": segment_id,
            "callfrom": "8015",
            "callto": "8014",
            "speaker": speaker,
            "direction": session.direction,
            "text": text,
        }

    async def scenario():
        for session, speaker, segment_id in [
            (caller, "caller", "caller-0001"),
            (agent, "agent", "agent-0001"),
        ]:
            await session._publish_completed_turn_text(CompletedTurnRecording(
                call_id=session.call_id,
                callfrom="8015",
                callto="8014",
                speaker=speaker,
                direction="inbound" if speaker == "caller" else "outbound",
                segment_ids=(segment_id,),
                pcm=b"\x01\x00" * 1600,
                start_time_ms=100,
                end_time_ms=900,
            ))
            await session._drain_background_tasks()

    asyncio.run(scenario())

    corrections = [event for event in broadcast_events if event["event"] == "call.corrected"]
    assert [event["callId"] for event in corrections] == ["caller-call", "agent-call"]
    assert [event["correctionScope"] for event in corrections] == ["turn", "turn"]
    assert corrections[0]["turns"][0]["speaker"] == "caller"
    assert corrections[1]["turns"][0]["speaker"] == "agent"



def test_audio_frame_broadcasts_vad_events_to_monitor(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    client = FakeAckClientWs()
    upstream = FakeSendUpstreamWs()
    session = BridgeSession(client, upstream, "call-2", callfrom="8001", callto="8002")
    session._vad = FakeVAD([
        FakeVADResult(is_speaking=True, speech_started=True, silence_duration_ms=0),
        FakeVADResult(is_speaking=False, speech_ended=True, silence_duration_ms=1500),
    ])

    first_event, first_pcm = make_audio_event(seq=1, speaker="caller")
    second_event, second_pcm = make_audio_event(seq=2, speaker="caller")

    asyncio.run(session._on_audio_frame(first_event))
    asyncio.run(session._on_audio_frame(second_event))

    assert upstream.sent_bytes == [first_pcm, second_pcm]
    assert [event["event"] for event in events] == ["speech.vad", "speech.vad"]
    assert events[0]["vadState"] == "speaking"
    assert events[0]["speaker"] == "caller"
    assert events[1]["vadState"] == "ended"
    assert events[1]["silenceDurationMs"] == 1500
    assert isinstance(events[0]["sendTimeMs"], int)
    assert "audioLevel" in events[0]
    assert "volumeDb" in events[0]


def test_audio_frame_sends_preprocessed_pcm_to_funasr_and_raw_pcm_to_vad(monkeypatch):
    async def fake_broadcast(event, **kwargs):
        pass

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    upstream = FakeSendUpstreamWs()
    session = BridgeSession(FakeAckClientWs(), upstream, "call-gain", callfrom="8001", callto="8002")
    session._vad = FakeVAD([FakeVADResult(is_speaking=True, speech_started=True)])
    quiet_pcm = (300).to_bytes(2, "little", signed=True) * 1600
    event, _ = make_audio_event(seq=1, speaker="caller", pcm=quiet_pcm, start_ms=0, end_ms=100)

    asyncio.run(session._on_audio_frame(event))

    assert upstream.sent_bytes[0] != quiet_pcm
    assert max_abs_sample(upstream.sent_bytes[0]) > max_abs_sample(quiet_pcm)
    assert session._vad.fed[0] == quiet_pcm


def test_audio_frame_logs_seq_and_timestamp_diagnostics(monkeypatch, caplog):
    async def fake_broadcast(event, **kwargs):
        pass

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-diag", callfrom="8001", callto="8002")
    session._vad = FakeVAD([FakeVADResult(), FakeVADResult()])
    pcm = (800).to_bytes(2, "little", signed=True) * 1600
    first, _ = make_audio_event(seq=1, speaker="caller", pcm=pcm, start_ms=0, end_ms=100)
    second, _ = make_audio_event(seq=3, speaker="caller", pcm=pcm, start_ms=400, end_ms=500)

    asyncio.run(session._on_audio_frame(first))
    with caplog.at_level("WARNING", logger="asr_bridge"):
        asyncio.run(session._on_audio_frame(second))

    assert "audio diagnostics" in caplog.text
    assert "seq gap" in caplog.text
    assert "timestamp gap" in caplog.text


class FakeSavedRecording:
    def __init__(self, audio_url="/recordings/2026-06-12/call-2/caller-0001.wav", duration_ms=1200):
        self.audio_url = audio_url
        self.record_id = "rec-test-1"
        self.duration_ms = duration_ms
        self.start_time_ms = 800
        self.end_time_ms = 2000


class FakeRecordingStore:
    def __init__(self):
        self.saved = []

    def save_segment(self, pcm, **metadata):
        self.saved.append((pcm, metadata))
        return FakeSavedRecording()


def test_vad_end_saves_audio_without_waiting_for_peer_effective_speech(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = FakeRecordingStore()
    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-2", callfrom="8001", callto="8002", recording_store=recorder)
    session.speaker = "caller"
    session.direction = "inbound"
    session.current_segment_id = "caller-0001"
    session.turn_end_ms = 2000
    pcm = b"\x01\x00" * 19200
    session._vad = FakeVAD([FakeVADResult(is_speaking=False, speech_ended=True, silence_duration_ms=1500, audio_segment=pcm)])

    async def scenario():
        await session._process_vad_frame(b"\0" * 640)
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert recorder.saved[0][0] == pcm
    assert [event["event"] for event in events] == ["speech.vad", "audio.segment"]
    assert events[0]["vadState"] == "ended"

def test_call_end_flushes_residual_audio_before_call_ended(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = FakeRecordingStore()
    client = FakeAckClientWs()
    session = BridgeSession(client, FakeSendUpstreamWs(), "call-3", callfrom="8001", callto="8002", recording_store=recorder)
    session.speaker = "agent"
    session.turn_end_ms = 2400
    residual = b"\x02\x00" * 8000
    session._vad.flush = lambda: residual

    async def scenario():
        await session._on_call_ended({"seq": 9})
        assert [event["event"] for event in events] == ["call.ended"]
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert recorder.saved[0][0] == residual
    assert [event["event"] for event in events] == ["call.ended", "audio.segment"]
    assert client.sent[-1]["receivedSeq"] == 9


def test_vad_end_closes_current_funasr_segment_and_saves_audio(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = FakeRecordingStore()
    upstream = FakeSendUpstreamWs()
    session = BridgeSession(FakeAckClientWs(), upstream, "call-segment", recording_store=recorder)
    session.speaker = "agent"
    session.handshake_sent = True
    session.current_segment_id = "agent-0001"
    session._vad = FakeVAD([FakeVADResult(
        is_speaking=False,
        speech_ended=True,
        silence_duration_ms=800,
        audio_segment=b"\x01\x00" * 16000,
    )])

    async def scenario():
        await session._process_vad_frame(b"\0" * 640)
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert recorder.saved[0][0] == b"\x01\x00" * 16000
    assert events[0]["event"] == "speech.vad"
    assert events[-1]["event"] == "audio.segment"
    assert upstream.sent_text[-1]["is_speaking"] is False
    assert session.handshake_sent is False


def test_pending_completed_turn_is_retried_when_text_arrives_on_peer_session(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    asr_bridge._global_speech_events.clear()
    asr_bridge._global_pending_completed_texts.clear()

    completed = CompletedTurnRecording(
        call_id="agent-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
        direction="outbound",
        segment_ids=("agent-0001",),
        pcm=b"q-audio",
        start_time_ms=0,
        end_time_ms=1200,
    )
    caller_session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "caller-call", callfrom="8001", callto="8002")
    asyncio.run(caller_session._publish_or_defer_completed_turn_text(completed))

    assert len(asr_bridge._global_pending_completed_texts) == 1
    assert not [event for event, kwargs in events if event.get("finalSource")]

    agent_session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "agent-call", callfrom="8001", callto="8002")
    agent_session.speaker = "agent"
    agent_session.direction = "outbound"
    asyncio.run(agent_session._broadcast_speech_to_monitors("哦那你那边还有什么其他情况吗", segment_id="agent-0001"))

    stable_events = [event for event, kwargs in events if event.get("finalSource")]
    assert stable_events[-1]["callId"] == "agent-call"
    assert stable_events[-1]["segmentIds"] == ["agent-0001"]
    assert stable_events[-1]["text"] == "哦那你那边还有什么其他情况吗"
    assert asr_bridge._global_pending_completed_texts == {}

def test_peer_vad_start_completes_previous_side_across_sessions(monkeypatch):
    from turn_recording_coordinator import TurnRecordingCoordinator

    events = []

    async def fake_broadcast(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    asr_bridge._global_speech_events.clear()

    coordinator = TurnRecordingCoordinator()
    recorder = FakeRecordingStore()
    agent = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "agent-call",
        callfrom="8001", callto="8002", recording_store=recorder,
        turn_recording_coordinator=coordinator,
    )
    agent.speaker = "agent"
    agent.direction = "outbound"
    agent.current_segment_id = "agent-0001"
    agent.turn_start_ms = 0
    agent.turn_end_ms = 1000

    asyncio.run(agent._broadcast_speech_to_monitors("喂你好这里是119", segment_id="agent-0001"))
    agent._vad = FakeVAD([FakeVADResult(
        is_speaking=False, speech_ended=True, silence_duration_ms=1300, audio_segment=b"q-audio",
    )])
    async def scenario():
        await agent._process_vad_frame(b"\0" * 640)
        final_events = [event for event, kwargs in events if event["event"] == "speech.final" and event.get("finalSource")]
        assert final_events[-1]["callId"] == "agent-call"
        assert final_events[-1]["text"] == "喂你好这里是119"
        assert final_events[-1]["segmentIds"] == ["agent-0001"]
        await agent._drain_background_tasks()

    asyncio.run(scenario())

    assert recorder.saved[0][0] == b"q-audio"
    audio_events = [event for event, kwargs in events if event["event"] == "audio.segment"]
    assert audio_events[-1]["callId"] == "agent-call"


def test_each_vad_ended_segment_saves_its_own_turn_recording(monkeypatch):
    from turn_recording_coordinator import TurnRecordingCoordinator

    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    coordinator = TurnRecordingCoordinator()
    recorder = FakeRecordingStore()
    caller = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "caller-call",
        callfrom="8001", callto="8002", recording_store=recorder,
        turn_recording_coordinator=coordinator,
    )
    caller.speaker = "caller"
    caller.direction = "inbound"
    caller.turn_start_ms = 0
    caller.turn_end_ms = 1000
    caller.current_segment_id = "caller-0001"
    first_pcm = b"a" * 10
    second_pcm = b"b" * 12

    async def scenario():
        caller._vad = FakeVAD([FakeVADResult(is_speaking=False, speech_ended=True, silence_duration_ms=1000, audio_segment=first_pcm)])
        await caller._process_vad_frame(b"\0" * 640)

        caller.turn_start_ms = 1200
        caller.turn_end_ms = 2200
        caller.current_segment_id = "caller-0002"
        caller._vad = FakeVAD([FakeVADResult(is_speaking=False, speech_ended=True, silence_duration_ms=1000, audio_segment=second_pcm)])
        await caller._process_vad_frame(b"\0" * 640)
        await caller._drain_background_tasks()

    asyncio.run(scenario())

    assert [item[0] for item in recorder.saved] == [first_pcm, second_pcm]
    assert [item[1]["call_id"] for item in recorder.saved] == ["caller-call", "caller-call"]
    assert [item[1]["speaker"] for item in recorder.saved] == ["caller", "caller"]
    audio_events = [event for event in events if event["event"] == "audio.segment"]
    assert len(audio_events) == 2
    assert [event["callId"] for event in audio_events] == ["caller-call", "caller-call"]
    assert [event["segmentId"] for event in audio_events] == ["caller-0001", "caller-0002"]
    assert [event["segmentIds"] for event in audio_events] == [["caller-0001"], ["caller-0002"]]


def test_funasr_wav_name_segment_id_is_forwarded_to_monitor(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    upstream = FakeUpstreamWs([FakeMessage(json.dumps({
        "mode": "streaming",
        "text": "第二问。",
        "is_final": True,
        "wav_name": "call-segment__agent-0002",
    }, ensure_ascii=False))])
    session = BridgeSession(FakeClientWs(), upstream, "call-segment")
    session.speaker = "agent"
    session._valid_segment_ids.add("agent-0002")

    asyncio.run(session._upstream_to_client())

    assert events[0]["event"] == "speech.final"
    assert events[0]["callId"] == "call-segment"
    assert events[0]["segmentId"] == "agent-0002"


def test_bridge_uses_less_fragmented_vad_defaults():
    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-config")

    assert session._vad.config.speech_confirm_frames == 4
    assert session._vad.config.silence_confirm_ms == 1300
    assert session._vad.config.min_speech_ms == 500
    assert session._vad.config.vad_aggressiveness == 3
    assert session._vad.config.energy_silence_db == -42.0
    assert session._vad_use_raw_audio is True


def test_next_vad_segment_uses_a_fresh_upstream_connection():
    first = FakeSendUpstreamWs()
    second = FakeSendUpstreamWs()
    connected = []

    async def connect_upstream():
        connected.append(second)
        return second

    session = BridgeSession(
        FakeAckClientWs(), first, "call-fresh", upstream_factory=connect_upstream
    )
    session.speaker = "agent"

    asyncio.run(session._start_funasr())
    asyncio.run(session._stop_funasr())
    asyncio.run(session._start_funasr())

    assert connected == [second]
    assert session.upstream_ws is second
    assert first.sent_text[0]["wav_name"].endswith("agent-0001")
    assert second.sent_text[0]["wav_name"].endswith("agent-0002")


def test_addressbot_handshake_uses_address_hotwords(tmp_path):
    write_hotword_fixture(tmp_path)
    upstream = FakeSendUpstreamWs()
    manager = HotwordManager(project="addressbot", hotword_dir=tmp_path, mode="dynamic")
    session = BridgeSession(
        FakeAckClientWs(),
        upstream,
        "call-address-hotwords",
        project="addressbot",
        hotword_manager=manager,
    )
    session.speaker = "caller"

    asyncio.run(session._start_funasr())

    assert upstream.sent_text[0]["hotwords"] == "科兴科学园 科苑北路"


def test_stage_changed_event_switches_next_handshake_hotwords(tmp_path):
    write_hotword_fixture(tmp_path)
    upstream = FakeSendUpstreamWs()
    manager = HotwordManager(project="addressbot", hotword_dir=tmp_path, mode="dynamic")
    session = BridgeSession(
        FakeAckClientWs(),
        upstream,
        "call-stage-hotwords",
        project="addressbot",
        hotword_manager=manager,
    )

    asyncio.run(session._dispatch_client_msg(FakeMessage(json.dumps({
        "eventType": "stage.changed",
        "seq": 9,
        "stage": "inquiry_base",
    }, ensure_ascii=False))))
    session.speaker = "caller"
    asyncio.run(session._start_funasr())

    assert manager.stage == HotwordStage.INQUIRY_BASE
    assert upstream.sent_text[0]["hotwords"] == "着火 冒烟"


def test_scene_signal_switches_hotwords_on_the_next_funasr_segment():
    first = FakeSendUpstreamWs()
    second = FakeSendUpstreamWs()

    async def connect_upstream():
        return second

    client = FakeAckClientWs()
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=(
            "/home/twai/wjl/DynamicHotwordLoading/hotwords"
        ),
    )
    session = BridgeSession(
        client,
        first,
        "call-scene-signal",
        upstream_factory=connect_upstream,
        hotword_manager=manager,
    )
    session.speaker = "caller"

    asyncio.run(session._start_funasr())
    first_hotwords = first.sent_text[0]["hotwords"].split()
    assert "classification_assist.call_type" in manager.library_ids

    asyncio.run(
        session._dispatch_client_msg(
            FakeMessage(
                json.dumps(
                    {
                        "eventType": "scene_signal.add",
                        "seq": 9,
                        "signals": {
                            "call_type": ["fire_fighting"],
                            "building_structure": [
                                "highrise_multistory"
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            )
        )
    )

    assert first.sent_text[0]["hotwords"].split() == first_hotwords
    assert client.sent[-1]["accepted"] is True
    assert manager.hotword_version == 2
    asyncio.run(session._stop_funasr())
    asyncio.run(session._start_funasr())

    second_hotwords = second.sent_text[0]["hotwords"].split()
    assert "煤气罐" in second_hotwords
    assert "call_type.fire_fighting" in manager.library_ids
    assert "building_structure.highrise_multistory" in manager.library_ids
    assert "classification_assist.call_type" not in manager.library_ids


def test_invalid_scene_signal_is_rejected_without_changing_hotwords():
    client = FakeAckClientWs()
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=(
            "/home/twai/wjl/DynamicHotwordLoading/hotwords"
        ),
    )
    original_hotwords = manager.current_hotwords()
    session = BridgeSession(
        client,
        FakeSendUpstreamWs(),
        "call-invalid-scene-signal",
        hotword_manager=manager,
    )

    asyncio.run(
        session._dispatch_client_msg(
            FakeMessage(
                json.dumps(
                    {
                        "type": "scene_signal.add",
                        "seq": 10,
                        "signals": {
                            "building_structure": ["not-a-structure"]
                        },
                    },
                    ensure_ascii=False,
                )
            )
        )
    )

    assert client.sent[-1]["accepted"] is False
    assert client.sent[-1]["message"] == "INVALID_SCENE_SIGNAL"
    assert manager.current_hotwords() == original_hotwords
    assert manager.hotword_version == 1


def test_scene_text_switches_next_handshake_to_scene_hotwords(tmp_path, monkeypatch):
    write_hotword_fixture(tmp_path)
    broadcast_events = []

    async def fake_broadcast(event, **kwargs):
        broadcast_events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)

    upstream = FakeUpstreamWs([
        FakeMessage(json.dumps({
            "mode": "2pass-offline",
            "text": "二十楼的高层建筑正在冒烟",
            "is_final": True,
            "wav_name": "call-scene-hotwords__caller-0001",
        }, ensure_ascii=False))
    ])
    send_upstream = FakeSendUpstreamWs()
    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="dynamic")
    session = BridgeSession(
        FakeAckClientWs(),
        send_upstream,
        "call-scene-hotwords",
        hotword_manager=manager,
    )
    session.upstream_ws = upstream
    session.speaker = "caller"
    session._valid_segment_ids.add("caller-0001")

    asyncio.run(session._upstream_to_client())
    session.upstream_ws = send_upstream
    asyncio.run(session._start_funasr())

    assert manager.stage == HotwordStage.HIGHRISE
    assert send_upstream.sent_text[0]["hotwords"] == "高层建筑 超高层"
    assert any(event["text"] == "二十楼的高层建筑正在冒烟" for event in broadcast_events)


def test_unvoiced_funasr_segment_is_not_broadcast_to_monitor(monkeypatch):
    events = []

    async def fake_broadcast(event, **kwargs):
        events.append(event)

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    upstream = FakeUpstreamWs([FakeMessage(json.dumps({
        "mode": "streaming",
        "text": "上一段残留文本。",
        "is_final": True,
        "wav_name": "call-empty__agent-0003",
    }, ensure_ascii=False))])
    session = BridgeSession(FakeClientWs(), upstream, "call-empty")
    session.speaker = "agent"

    asyncio.run(session._upstream_to_client())

    assert events == []


def test_speech_broadcast_does_not_wait_for_database_remember(monkeypatch):
    order = []

    async def fake_broadcast(event, **kwargs):
        order.append("broadcast")

    class BlockingDatabase:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.events = []

        def remember_speech(self, event):
            order.append("database-start")
            self.started.set()
            self.release.wait(timeout=2)
            self.events.append(dict(event))
            order.append("database-end")

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    db = BlockingDatabase()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-fast-db",
        callfrom="8001", callto="8002", database_writer=db,
    )
    session.speaker = "caller"

    async def scenario():
        await session._broadcast_speech_to_monitors("我这里发生了火灾", segment_id="caller-0001")
        assert order == ["broadcast"]
        assert await asyncio.to_thread(db.started.wait, 1)
        assert order[:2] == ["broadcast", "database-start"]
        db.release.set()
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert order == ["broadcast", "database-start", "database-end"]
    assert db.events[0]["text"] == "我这里发生了火灾"


def test_completed_audio_save_runs_in_background(monkeypatch):
    events = []

    async def fake_broadcast(event):
        events.append(event)

    class BlockingRecorder:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def save_segment(self, pcm, **metadata):
            self.started.set()
            self.release.wait(timeout=2)
            class Saved:
                record_id = "uploaded-background"
                audio_url = "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/uploaded-background"
                duration_ms = 1000
                start_time_ms = 0
                end_time_ms = 1000
            return Saved()

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = BlockingRecorder()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-fast-audio",
        recording_store=recorder,
    )
    completed = CompletedTurnRecording(
        call_id="call-fast-audio",
        callfrom="8001",
        callto="8002",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"abc",
        start_time_ms=0,
        end_time_ms=1000,
    )

    async def scenario():
        session._schedule_completed_audio_save(completed)
        assert events == []
        assert await asyncio.to_thread(recorder.started.wait, 1)
        assert events == []
        recorder.release.set()
        await session._drain_background_tasks()

    asyncio.run(scenario())

    assert events[0]["event"] == "audio.segment"
    assert events[0]["recordId"] == "uploaded-background"

def test_audio_event_uses_uploaded_url_before_broadcast(monkeypatch):
    from turn_recording_coordinator import CompletedTurnRecording

    events = []

    async def fake_broadcast(event):
        events.append(event)

    class UploadingRecorder:
        def __init__(self):
            self.saved = []

        def save_segment(self, pcm, **metadata):
            self.saved.append((pcm, metadata))
            class Saved:
                record_id = "uploaded-rec-1"
                audio_url = "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/uploaded-rec-1"
                duration_ms = 1000
                start_time_ms = 0
                end_time_ms = 1000
            return Saved()

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    recorder = UploadingRecorder()
    session = BridgeSession(FakeAckClientWs(), FakeSendUpstreamWs(), "call-upload", recording_store=recorder)
    completed = CompletedTurnRecording(
        call_id="call-upload",
        callfrom="8001",
        callto="8002",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"abc",
        start_time_ms=0,
        end_time_ms=1000,
    )

    asyncio.run(session._save_and_broadcast_completed_audio(completed))

    assert recorder.saved[0][0] == b"abc"
    assert events[0]["event"] == "audio.segment"
    assert events[0]["recordId"] == "uploaded-rec-1"
    assert events[0]["audioUrl"] == "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/uploaded-rec-1"

def test_audio_event_does_not_fallback_to_local_audio_url_when_upload_fails(monkeypatch):
    from turn_recording_coordinator import CompletedTurnRecording

    events = []

    async def fake_broadcast(event):
        events.append(event)

    class FailingRemoteRecorder:
        def save_segment(self, pcm, **metadata):
            raise RuntimeError("upload unavailable")

    class CapturingDatabase:
        def __init__(self):
            self.audio_events = []

        def save_audio_turn(self, event):
            self.audio_events.append(dict(event))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    db = CapturingDatabase()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-upload-fail",
        recording_store=FailingRemoteRecorder(), database_writer=db,
    )
    completed = CompletedTurnRecording(
        call_id="call-upload-fail",
        callfrom="8001",
        callto="8002",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"abc",
        start_time_ms=0,
        end_time_ms=1000,
    )

    asyncio.run(session._save_and_broadcast_completed_audio(completed))

    assert events[0]["event"] == "audio.segment"
    assert events[0]["recordId"] is None
    assert events[0]["audioUrl"] == ""
    assert events[0]["localAudioUrl"].startswith("/recordings/")
    assert db.audio_events[0]["audioUrl"] == ""


def test_audio_database_persists_uploaded_url_before_broadcast(monkeypatch):
    from turn_recording_coordinator import CompletedTurnRecording

    order = []
    events = []

    async def fake_broadcast(event):
        order.append("broadcast")
        events.append(event)

    class UploadingRecorder:
        def save_segment(self, pcm, **metadata):
            order.append("upload")
            class Saved:
                record_id = "uploaded-rec-order"
                audio_url = "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/uploaded-rec-order"
                duration_ms = 1000
                start_time_ms = 0
                end_time_ms = 1000
            return Saved()

    class CapturingDatabase:
        def __init__(self):
            self.audio_events = []

        def save_audio_turn(self, event):
            order.append("database")
            self.audio_events.append(dict(event))

    monkeypatch.setattr(asr_bridge, "_broadcast_to_monitors", fake_broadcast)
    db = CapturingDatabase()
    session = BridgeSession(
        FakeAckClientWs(), FakeSendUpstreamWs(), "call-upload-order",
        recording_store=UploadingRecorder(), database_writer=db,
    )
    completed = CompletedTurnRecording(
        call_id="call-upload-order",
        callfrom="8001",
        callto="8002",
        speaker="caller",
        direction="inbound",
        segment_ids=("caller-0001",),
        pcm=b"abc",
        start_time_ms=0,
        end_time_ms=1000,
    )

    asyncio.run(session._save_and_broadcast_completed_audio(completed))

    assert order == ["upload", "database", "broadcast"]
    assert db.audio_events[0]["audioUrl"] == "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/uploaded-rec-order"
    assert events[0]["audioUrl"] == db.audio_events[0]["audioUrl"]



class PairedSwitchSession:
    call_ended = False

    def __init__(
        self,
        call_id,
        speaker,
        *,
        project="firebot",
        callfrom="13900000000",
        callto="8001",
    ):
        self.call_id = call_id
        self.speaker = speaker
        self.project = project
        self.callfrom = callfrom
        self.callto = callto
        self.switch_requests = []

    async def request_model_switch(self, target_provider, request_id):
        self.switch_requests.append((target_provider, request_id))
        return {"accepted": True}


def test_switch_paired_sessions_resolves_caller_from_agent_call_id():
    agent = PairedSwitchSession("agent-stream", "agent")
    caller = PairedSwitchSession("caller-stream", "caller")

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions.update({
            agent.call_id: agent,
            caller.call_id: caller,
        })
        try:
            return await asr_bridge.switch_paired_session_models(
                agent.call_id,
                "8001",
                "xfyun",
                "req-http",
            )
        finally:
            asr_bridge._active_sessions.clear()

    result = asyncio.run(scenario())

    assert result["accepted"] is True
    assert result["acceptedCallIds"] == ["agent-stream", "caller-stream"]
    assert agent.switch_requests == [("xfyun", "req-http")]
    assert caller.switch_requests == [("xfyun", "req-http")]


def test_switch_paired_sessions_resolves_agent_from_caller_call_id():
    agent = PairedSwitchSession("agent-stream", "agent")
    caller = PairedSwitchSession("caller-stream", "caller")

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions.update({
            agent.call_id: agent,
            caller.call_id: caller,
        })
        try:
            return await asr_bridge.switch_paired_session_models(
                caller.call_id,
                "8001",
                "xfyun",
                "req-caller-anchor",
            )
        finally:
            asr_bridge._active_sessions.clear()

    result = asyncio.run(scenario())

    assert result["accepted"] is True
    assert result["acceptedCallIds"] == ["agent-stream", "caller-stream"]
    assert agent.switch_requests == [("xfyun", "req-caller-anchor")]
    assert caller.switch_requests == [("xfyun", "req-caller-anchor")]


def test_switch_paired_sessions_rejects_wrong_seat():
    agent = PairedSwitchSession("agent-stream", "agent")
    caller = PairedSwitchSession("caller-stream", "caller")

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions.update({
            agent.call_id: agent,
            caller.call_id: caller,
        })
        try:
            return await asr_bridge.switch_paired_session_models(
                caller.call_id,
                "8002",
                "xfyun",
                "req-wrong-seat",
            )
        finally:
            asr_bridge._active_sessions.clear()

    result = asyncio.run(scenario())

    assert result["message"] == "SEAT_ID_MISMATCH"
    assert agent.switch_requests == []
    assert caller.switch_requests == []


def test_switch_paired_sessions_rejects_unknown_stream_role():
    unknown = PairedSwitchSession("unknown-stream", "unknown")

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions[unknown.call_id] = unknown
        try:
            return await asr_bridge.switch_paired_session_models(
                unknown.call_id,
                "8001",
                "xfyun",
                "req-unknown-role",
            )
        finally:
            asr_bridge._active_sessions.clear()

    result = asyncio.run(scenario())

    assert result["message"] == "CALL_ID_NOT_SWITCHABLE_STREAM"
    assert unknown.switch_requests == []


def test_switch_paired_sessions_rejects_missing_or_ambiguous_caller():
    agent = PairedSwitchSession("agent-stream", "agent")
    caller_a = PairedSwitchSession("caller-a", "caller")
    caller_b = PairedSwitchSession("caller-b", "caller")

    async def scenario():
        asr_bridge._active_sessions.clear()
        asr_bridge._active_sessions[agent.call_id] = agent
        try:
            missing = await asr_bridge.switch_paired_session_models(
                agent.call_id,
                "8001",
                "xfyun",
                "req-missing",
            )
            asr_bridge._active_sessions.update({
                caller_a.call_id: caller_a,
                caller_b.call_id: caller_b,
            })
            ambiguous = await asr_bridge.switch_paired_session_models(
                agent.call_id,
                "8001",
                "xfyun",
                "req-ambiguous",
            )
            return missing, ambiguous
        finally:
            asr_bridge._active_sessions.clear()

    missing, ambiguous = asyncio.run(scenario())

    assert missing["message"] == "PAIRED_STREAM_NOT_FOUND"
    assert ambiguous["message"] == "PAIRED_STREAM_AMBIGUOUS"
    assert agent.switch_requests == []
