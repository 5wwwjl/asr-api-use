import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp import WSMsgType


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_providers import (  # noqa: E402
    ASRProviderFactory,
    FunASRProvider,
    XfyunDialectConfig,
    XfyunDialectProvider,
)


def _xfyun_response(*, sn: int, words: list[str], status: int) -> dict:
    result = {
        "sn": sn,
        "ls": status == 2,
        "pgs": "apd",
        "ws": [{"cw": [{"w": word}]} for word in words],
    }
    return {
        "header": {"code": 0, "message": "success", "sid": "sid-live", "status": status},
        "payload": {
            "result": {
                "encoding": "utf8",
                "compress": "raw",
                "format": "json",
                "status": status,
                "seq": sn,
                "text": base64.b64encode(
                    json.dumps(result, ensure_ascii=False).encode("utf-8")
                ).decode("ascii"),
            }
        },
    }


class FakeXfyunWs:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.closed = False

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))

    async def receive(self):
        if self.responses:
            return SimpleNamespace(
                type=WSMsgType.TEXT,
                data=json.dumps(self.responses.pop(0), ensure_ascii=False),
            )
        return SimpleNamespace(type=WSMsgType.CLOSED, data="")

    async def close(self):
        self.closed = True


class TimedFakeXfyunWs(FakeXfyunWs):
    def __init__(self):
        super().__init__([])
        self.sent_at = []

    async def send_str(self, payload):
        self.sent_at.append(time.monotonic())
        await super().send_str(payload)


class FakeHttpClient:
    def __init__(self, ws):
        self.ws = ws
        self.urls = []

    async def ws_connect(self, url, **kwargs):
        self.urls.append(url)
        return self.ws


class FakeFunAsrWs:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent_text = []
        self.sent_bytes = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send_str(self, payload):
        self.sent_text.append(json.loads(payload))

    async def send_bytes(self, payload):
        self.sent_bytes.append(payload)

    async def close(self):
        self.closed = True


def test_xfyun_config_requires_feature_flag_and_all_credentials():
    disabled = XfyunDialectConfig.from_mapping({
        "ASR_XFYUN_ENABLED": "false",
        "XFYUN_APP_ID": "app",
        "XFYUN_API_KEY": "key",
        "XFYUN_API_SECRET": "secret",
    })
    missing = XfyunDialectConfig.from_mapping({"ASR_XFYUN_ENABLED": "true"})
    enabled = XfyunDialectConfig.from_mapping({
        "ASR_XFYUN_ENABLED": "true",
        "XFYUN_APP_ID": "app",
        "XFYUN_API_KEY": "key",
        "XFYUN_API_SECRET": "secret",
    })

    assert disabled.availability() == (False, "XFYUN_DISABLED")
    assert missing.availability() == (False, "XFYUN_CREDENTIALS_MISSING")
    assert enabled.availability() == (True, "")


def test_xfyun_provider_streams_status_frames_and_emits_final_text():
    ws = FakeXfyunWs([
        _xfyun_response(sn=0, words=["贵", "阳"], status=1),
        _xfyun_response(sn=1, words=["着火了"], status=2),
    ])
    client = FakeHttpClient(ws)
    config = XfyunDialectConfig(
        enabled=True,
        app_id="app",
        api_key="key",
        api_secret="secret",
    )
    provider = XfyunDialectProvider(
        client,
        config,
        segment_id="caller-0001",
        frame_interval_seconds=0,
    )

    async def scenario():
        await provider.start()
        collector = asyncio.create_task(_collect(provider.events()))
        await provider.send_audio(b"\x01\x00" * 640)
        await provider.finish()
        return await collector

    events = asyncio.run(scenario())

    assert [item["header"]["status"] for item in ws.sent] == [0, 2]
    assert ws.sent[0]["parameter"]["iat"]["accent"] == "mulacc"
    assert [event.text for event in events] == ["贵阳", "贵阳着火了"]
    assert events[-1].is_final is True
    assert events[-1].provider == "xfyun"
    assert events[-1].segment_id == "caller-0001"
    assert "secret" not in client.urls[0]


def test_xfyun_provider_replays_large_buffer_without_dropping_frames():
    ws = FakeXfyunWs([])
    client = FakeHttpClient(ws)
    config = XfyunDialectConfig(
        enabled=True,
        app_id="app",
        api_key="key",
        api_secret="secret",
    )
    provider = XfyunDialectProvider(
        client,
        config,
        segment_id="caller-buffered",
        frame_interval_seconds=0,
    )
    frame_size = 1280
    frames = [
        bytes([1]) * frame_size,
        bytes([2]) * frame_size,
        bytes([3]) * frame_size,
    ]

    async def scenario():
        await provider.start()
        await provider.send_audio(b"".join(frames))
        await provider.finish()

    asyncio.run(scenario())

    audio_messages = [
        item for item in ws.sent
        if item["header"]["status"] in {0, 1}
    ]
    sent_frames = [
        base64.b64decode(item["payload"]["audio"]["audio"])
        for item in audio_messages
    ]
    assert sent_frames == frames
    assert [item["header"]["status"] for item in ws.sent] == [0, 1, 1, 2]


def test_xfyun_buffered_replay_starts_pacing_from_first_audio_frame():
    ws = TimedFakeXfyunWs()
    client = FakeHttpClient(ws)
    config = XfyunDialectConfig(
        enabled=True,
        app_id="app",
        api_key="key",
        api_secret="secret",
    )
    provider = XfyunDialectProvider(
        client,
        config,
        segment_id="caller-buffered-pacing",
        frame_interval_seconds=0.01,
    )
    frame = b"\x01\x00" * 640

    async def scenario():
        await provider.start()
        await asyncio.sleep(0.05)
        await provider.send_audio(frame * 4)
        await provider.finish()

    asyncio.run(scenario())

    audio_send_times = [
        sent_at
        for sent_at, item in zip(ws.sent_at, ws.sent)
        if item["header"]["status"] in {0, 1}
    ]
    assert len(audio_send_times) == 4
    assert audio_send_times[-1] - audio_send_times[0] >= 0.025


def test_funasr_provider_keeps_existing_handshake_and_normalizes_results():
    ws = FakeFunAsrWs([
        SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "mode": "2pass-offline",
            "text": "科 兴 科 学 园",
            "wav_name": "call-1__caller-0001",
        }, ensure_ascii=False)),
    ])
    provider = FunASRProvider(
        ws,
        call_id="call-1",
        segment_id="caller-0001",
        hotwords="科兴科学园 科苑北路",
    )

    async def scenario():
        await provider.start()
        await provider.send_audio(b"pcm")
        await provider.finish()
        return await _collect(provider.events())

    events = asyncio.run(scenario())

    assert ws.sent_text[0]["wav_name"] == "call-1__caller-0001"
    assert ws.sent_text[0]["hotwords"] == "科兴科学园 科苑北路"
    assert ws.sent_bytes == [b"pcm"]
    assert ws.sent_text[-1] == {"is_speaking": False, "mode": "2pass"}
    assert events[-1].text == "科兴科学园"
    assert events[-1].is_final is True


def test_provider_factory_uses_initial_then_fresh_funasr_socket():
    first = FakeFunAsrWs()
    second = FakeFunAsrWs()
    connected = []

    async def connector():
        connected.append(True)
        return second

    factory = ASRProviderFactory(
        call_id="call-1",
        initial_funasr_ws=first,
        funasr_connector=connector,
        xfyun_client=FakeHttpClient(FakeXfyunWs([])),
        xfyun_config=XfyunDialectConfig(enabled=False),
    )

    async def scenario():
        one = await factory.create("funasr", "caller-0001", hotwords="")
        two = await factory.create("funasr", "caller-0002", hotwords="")
        return one, two

    one, two = asyncio.run(scenario())

    assert one.ws is first
    assert two.ws is second
    assert connected == [True]


async def _collect(iterator):
    return [item async for item in iterator]
