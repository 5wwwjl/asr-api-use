import json
import sys
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from https_gateway import (
    AUDIO_DATA_DIR_KEY,
    ASR_ACCURACY_BASELINE_UPSTREAM_WS_KEY,
    ASR_CPU_TEST_UPSTREAM_WS_KEY,
    ASR_DATABASE_READER_KEY,
    RECORDINGS_DIR_KEY,
    HOTWORD_DEMO_SERVICE_KEY,
    _bridge_call_id_from_first_message,
    _bridge_project_from_first_message,
    _bridge_required_error_payload,
    _handle_monitor_command,
    _normalize_asr_project,
    _prepare_direct_handshake,
    _prepare_direct_funasr_payload,
    _requested_hotword_mode,
    asr_model_switch,
    asr_records,
    create_app,
    cti_events,
    push_asr_records,
)


class FakeRequest:
    def __init__(self, path, query=None):
        self.path = path
        self.query = query or {}


def test_ab_endpoints_force_explicit_hotword_modes():
    assert _requested_hotword_mode(FakeRequest("/asr-plain")) == "off"
    assert _requested_hotword_mode(FakeRequest("/asr-dynamic")) == "scene_dynamic"
    assert _requested_hotword_mode(
        FakeRequest("/asr", {"hotwordMode": "off"})
    ) == "off"


def test_accuracy_a_native_handshake_never_injects_hotwords():
    raw = json.dumps({
        "wav_name": "accuracy-a",
        "is_speaking": True,
        "mode": "2pass",
    })

    outgoing, injected = _prepare_direct_handshake(raw, inject_hotwords=False)

    assert outgoing == raw
    assert injected is False
    assert "hotwords" not in json.loads(outgoing)


def test_offline_funasr_result_is_forwarded_as_final():
    state = {"sent_final_text": False}
    raw = json.dumps({
        "is_final": False,
        "mode": "2pass-offline",
        "text": "<|zh|>然后它一关掉的话，我这边可能又用不了。",
        "wav_name": "wav-default-id",
    }, ensure_ascii=False)

    payload = _prepare_direct_funasr_payload(raw, state)

    obj = json.loads(payload)
    assert obj["is_final"] is True
    assert obj["mode"] == "2pass-offline"
    assert obj["text"] == "然后它一关掉的话，我这边可能又用不了。"
    assert obj["callfrom"] == "micro"
    assert obj["callto"] == "micro"
    assert state["sent_final_text"] is True


def test_empty_final_terminator_is_suppressed_after_final_text():
    state = {"sent_final_text": False}
    final_text = json.dumps({
        "is_final": False,
        "mode": "2pass-offline",
        "text": "最终文本",
        "wav_name": "wav-default-id",
    }, ensure_ascii=False)
    empty_terminator = json.dumps({
        "is_final": True,
        "text": "",
        "wav_name": "wav-default-id",
    }, ensure_ascii=False)

    assert _prepare_direct_funasr_payload(final_text, state) is not None
    assert _prepare_direct_funasr_payload(empty_terminator, state) is None


def test_create_app_exposes_recordings_directory(tmp_path):
    recordings = tmp_path / "recordings"
    app = create_app(recordings_dir=recordings)

    assert recordings.is_dir()
    assert app[RECORDINGS_DIR_KEY] == recordings
    assert any(getattr(resource, "canonical", "") == "/recordings" for resource in app.router.resources())


def test_create_app_exposes_audio_data_directory_before_web_root(tmp_path):
    recordings = tmp_path / "recordings"
    audio_data = tmp_path / "audio_data"

    app = create_app(recordings_dir=recordings, audio_data_dir=audio_data)

    assert audio_data.is_dir()
    assert app[AUDIO_DATA_DIR_KEY] == audio_data
    resources = [
        getattr(resource, "canonical", "")
        for resource in app.router.resources()
    ]
    assert "/audio_data" in resources
    assert resources.index("/audio_data") < resources.index("")


def test_create_app_exposes_cti_events_endpoint(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")

    routes = [
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    ]

    assert ("POST", "/cti/events") in routes


def test_create_app_exposes_hotword_demo_endpoints(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")
    routes = {
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    }

    assert app[HOTWORD_DEMO_SERVICE_KEY] is not None
    assert ("POST", "/api/hotword-demo/start") in routes
    assert ("POST", "/api/hotword-demo/address") in routes
    assert ("POST", "/api/hotword-demo/scene") in routes
    assert ("POST", "/api/hotword-demo/compare") in routes
    assert ("GET", "/asr-plain") in routes
    assert ("GET", "/asr-dynamic") in routes


def test_create_app_exposes_asr_records_endpoint(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")

    routes = [
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    ]

    assert ("GET", "/asr/records") in routes
    assert ("POST", "/asr/records/push") in routes
    assert ("GET", "/asr/transcripts/{call_id}") in routes


def test_create_app_exposes_cpu_full_chain_test_endpoint(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")
    routes = [
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    ]

    assert ("GET", "/asr-cpu-test") in routes
    assert app[ASR_CPU_TEST_UPSTREAM_WS_KEY].endswith(":10097")


def test_create_app_exposes_native_accuracy_a_endpoint(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")
    routes = {
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    }

    assert ("GET", "/asr-accuracy-a") in routes
    assert app[ASR_ACCURACY_BASELINE_UPSTREAM_WS_KEY].endswith(":10098")
    assert app[ASR_CPU_TEST_UPSTREAM_WS_KEY].endswith(":10097")


def test_bridge_call_id_accepts_uppercase_call_id_field():
    first_obj = {
        "eventType": "call.started",
        "callId": "cti-call-id",
    }

    assert _bridge_call_id_from_first_message(first_obj, "query-call-id") == "cti-call-id"


def test_asr_project_defaults_to_firebot_unless_addressbot():
    assert _normalize_asr_project("") == "firebot"
    assert _normalize_asr_project("unknown-client") == "firebot"
    assert _normalize_asr_project("firebot") == "firebot"
    assert _normalize_asr_project("AddressBot") == "addressbot"
    assert _normalize_asr_project("address_bot") == "addressbot"


def test_bridge_project_accepts_message_field_before_query_fallback():
    first_obj = {
        "eventType": "call.started",
        "project": "AddressBot",
    }

    assert _bridge_project_from_first_message(first_obj, "firebot") == "addressbot"
    assert _bridge_project_from_first_message({}, "AddressBot") == "addressbot"
    assert _bridge_project_from_first_message({}, "") == "firebot"


def test_bridge_required_error_payload_explains_required_protocol():
    payload = _bridge_required_error_payload("call-1")

    assert payload["accepted"] is False
    assert payload["callId"] == "call-1"
    assert payload["message"] == "BRIDGE_PROTOCOL_REQUIRED"
    assert payload["requiredProtocol"] == "bridge"
    assert payload["expectedFirstEvent"] == "call.started"


def test_cti_events_forwards_payload_to_asr_hold_handler(monkeypatch):
    calls = []

    async def fake_apply(payload):
        calls.append(payload)
        return {"accepted": True, "changed": True}

    class FakeRequest:
        async def json(self):
            return {
                "data": {
                    "eventType": "localHoldCall",
                    "callId": "cti-call",
                    "ext": {"from": "8015", "to": "8014"},
                }
            }

    monkeypatch.setattr("https_gateway.apply_cti_hold_event", fake_apply)

    response = __import__("asyncio").run(cti_events(FakeRequest()))

    assert response.status == 200
    assert calls == [{
        "data": {
            "eventType": "localHoldCall",
            "callId": "cti-call",
            "ext": {"from": "8015", "to": "8014"},
        }
    }]


def test_asr_records_requires_call_id():
    class FakeRequest:
        query = {}
        match_info = {}

    response = __import__("asyncio").run(asr_records(FakeRequest()))

    assert response.status == 400
    assert json.loads(response.text)["message"] == "callId is required"


def test_asr_records_returns_database_records():
    class FakeReader:
        def __init__(self):
            self.calls = []

        def list_records(self, call_id, *, limit=None):
            self.calls.append((call_id, limit))
            return [{
                "callId": call_id,
                "segmentId": "caller-0001",
                "speaker": "caller",
                "text": "这里着火了",
            }]

    class FakeRequest:
        query = {"callId": "call-1", "limit": "20"}
        match_info = {}

        def __init__(self):
            self.reader = FakeReader()
            self.app = {ASR_DATABASE_READER_KEY: self.reader}

    request = FakeRequest()

    response = __import__("asyncio").run(asr_records(request))

    assert response.status == 200
    body = json.loads(response.text)
    assert body["success"] is True
    assert body["callId"] == "call-1"
    assert body["count"] == 1
    assert body["records"][0]["text"] == "这里着火了"
    assert request.reader.calls == [("call-1", 20)]


def test_asr_records_rejects_invalid_limit():
    class FakeRequest:
        query = {"callId": "call-1", "limit": "abc"}
        match_info = {}

    response = __import__("asyncio").run(asr_records(FakeRequest()))

    assert response.status == 400
    assert json.loads(response.text)["message"] == "limit must be an integer"


def test_push_asr_records_queries_database_and_broadcasts(monkeypatch):
    broadcasts = []

    async def fake_broadcast(call_id, records, *, callfrom="", callto=""):
        event = {
            "event": "call.history",
            "callId": call_id,
            "callfrom": callfrom,
            "callto": callto,
            "records": records,
            "count": len(records),
            "monitorCount": 0,
            "sendTimeMs": 123,
        }
        broadcasts.append(event)
        return event

    class FakeReader:
        def __init__(self):
            self.calls = []

        def list_records(self, call_id, *, limit=None):
            self.calls.append((call_id, limit))
            return [{
                "callId": call_id,
                "segmentId": "caller-0001",
                "speaker": "caller",
                "text": "这里着火了",
            }]

    class FakeRequest:
        def __init__(self):
            self.reader = FakeReader()
            self.app = {ASR_DATABASE_READER_KEY: self.reader}

        async def json(self):
            return {"callId": "call-1", "targetExt": "8016", "limit": 30}

    monkeypatch.setattr("https_gateway.broadcast_call_history", fake_broadcast)
    request = FakeRequest()

    response = __import__("asyncio").run(push_asr_records(request))

    assert response.status == 200
    body = json.loads(response.text)
    assert body["success"] is True
    assert body["pushed"] is True
    assert body["callId"] == "call-1"
    assert body["callto"] == "8016"
    assert body["count"] == 1
    assert body["monitorCount"] == 0
    assert body["event"]["event"] == "call.history"
    assert body["event"]["callto"] == "8016"
    assert body["event"]["records"][0]["text"] == "这里着火了"
    assert request.reader.calls == [("call-1", 30)]
    assert broadcasts[0]["callId"] == "call-1"


def test_push_asr_records_requires_call_id():
    class FakeRequest:
        async def json(self):
            return {}

    response = __import__("asyncio").run(push_asr_records(FakeRequest()))

    assert response.status == 400
    assert json.loads(response.text)["message"] == "callId is required"


def test_push_asr_records_rejects_invalid_json():
    class FakeRequest:
        async def json(self):
            raise ValueError("bad")

    response = __import__("asyncio").run(push_asr_records(FakeRequest()))

    assert response.status == 400
    assert json.loads(response.text)["message"] == "INVALID_JSON"


def test_monitor_model_switch_command_delegates_all_call_ids(monkeypatch):
    calls = []

    async def fake_switch(call_ids, target_provider, request_id):
        calls.append((call_ids, target_provider, request_id))
        return {
            "accepted": True,
            "acceptedCallIds": call_ids,
            "missingCallIds": [],
        }

    monkeypatch.setattr("https_gateway.switch_active_session_models", fake_switch)

    result = __import__("asyncio").run(_handle_monitor_command({
        "command": "asr.model.switch",
        "requestId": "req-1",
        "callIds": ["caller-stream", "agent-stream"],
        "targetProvider": "xfyun",
        "effective": "immediate",
    }))

    assert result["type"] == "command_result"
    assert result["accepted"] is True
    assert calls == [(["caller-stream", "agent-stream"], "xfyun", "req-1")]


def test_monitor_model_switch_command_rejects_invalid_payload():
    result = __import__("asyncio").run(_handle_monitor_command({
        "command": "asr.model.switch",
        "requestId": "",
        "callIds": [],
        "targetProvider": "unknown",
    }))

    assert result["accepted"] is False
    assert result["message"] == "INVALID_MODEL_SWITCH_COMMAND"


def test_monitor_model_switch_command_rejects_legacy_next_vad_effective():
    result = __import__("asyncio").run(_handle_monitor_command({
        "command": "asr.model.switch",
        "requestId": "req-legacy",
        "callIds": ["caller-stream", "agent-stream"],
        "targetProvider": "xfyun",
        "effective": "next_vad_segment",
    }))

    assert result["accepted"] is False
    assert result["message"] == "INVALID_MODEL_SWITCH_COMMAND"



def test_create_app_exposes_asr_model_switch_endpoint(tmp_path):
    app = create_app(recordings_dir=tmp_path / "recordings")
    routes = {
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    }

    assert ("POST", "/asr/model/switch") in routes
    assert ("OPTIONS", "/asr/model/switch") in routes


def assert_model_switch_envelope(body, *, success, message, code, data):
    assert set(body) == {"success", "message", "code", "data", "timestamp"}
    assert body["success"] is success
    assert body["message"] == message
    assert body["code"] == code
    assert body["data"] == data
    assert isinstance(body["timestamp"], int)
    assert body["timestamp"] > 0


def test_asr_model_switch_accepts_minimal_request_and_resolves_pair(monkeypatch):
    calls = []

    async def fake_switch(call_id, seat_id, target_provider, request_id):
        calls.append((call_id, seat_id, target_provider, request_id))
        return {
            "accepted": True,
            "acceptedCallIds": ["agent-stream", "caller-stream"],
            "message": "ok",
        }

    class FakeRequest:
        async def json(self):
            return {
                "callId": "agent-stream",
                "model": "xfyun",
                "seatId": "8001",
            }

    monkeypatch.setattr("https_gateway.switch_paired_session_models", fake_switch)
    response = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    request_id = body["data"]["requestId"]
    assert_model_switch_envelope(
        body,
        success=True,
        message="操作成功！",
        code=200,
        data={
            "requestId": request_id,
            "model": "xfyun",
            "callIds": ["agent-stream", "caller-stream"],
        },
    )
    assert request_id
    assert calls == [("agent-stream", "8001", "xfyun", request_id)]


def test_asr_model_switch_rejects_invalid_json():
    class FakeRequest:
        async def json(self):
            raise ValueError("bad json")

    response = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert_model_switch_envelope(
        body,
        success=False,
        message="INVALID_JSON",
        code=200,
        data=None,
    )
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_asr_model_switch_rejects_unknown_model():
    class FakeRequest:
        async def json(self):
            return {
                "callId": "agent-stream",
                "model": "unknown",
                "seatId": "8001",
            }

    response = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert_model_switch_envelope(
        body,
        success=False,
        message="INVALID_MODEL_SWITCH_REQUEST",
        code=200,
        data=None,
    )


def test_asr_model_switch_maps_missing_call_to_not_found(monkeypatch):
    async def fake_switch(call_id, seat_id, target_provider, request_id):
        return {
            "accepted": False,
            "requestId": request_id,
            "acceptedCallIds": [],
            "message": "CALL_NOT_FOUND",
        }

    class FakeRequest:
        async def json(self):
            return {
                "callId": "missing-agent-stream",
                "model": "funasr",
                "seatId": "8001",
            }

    monkeypatch.setattr("https_gateway.switch_paired_session_models", fake_switch)
    response = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    request_id = body["data"]["requestId"]
    assert_model_switch_envelope(
        body,
        success=False,
        message="CALL_NOT_FOUND",
        code=200,
        data={"requestId": request_id},
    )
    assert request_id


def test_asr_model_switch_maps_conflict_and_provider_unavailable(monkeypatch):
    current_message = {"value": "SEAT_ID_MISMATCH"}

    async def fake_switch(call_id, seat_id, target_provider, request_id):
        return {
            "accepted": False,
            "requestId": request_id,
            "acceptedCallIds": [],
            "message": current_message["value"],
        }

    class FakeRequest:
        async def json(self):
            return {
                "callId": "agent-stream",
                "model": "xfyun",
                "seatId": "8001",
            }

    monkeypatch.setattr("https_gateway.switch_paired_session_models", fake_switch)

    conflict = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    conflict_body = json.loads(conflict.text)
    assert conflict.status == 200
    assert_model_switch_envelope(
        conflict_body,
        success=False,
        message="SEAT_ID_MISMATCH",
        code=200,
        data={"requestId": conflict_body["data"]["requestId"]},
    )

    current_message["value"] = "XFYUN_UNAVAILABLE"
    unavailable = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    unavailable_body = json.loads(unavailable.text)
    assert unavailable.status == 200
    assert_model_switch_envelope(
        unavailable_body,
        success=False,
        message="XFYUN_UNAVAILABLE",
        code=200,
        data={"requestId": unavailable_body["data"]["requestId"]},
    )


def test_asr_model_switch_wraps_internal_error(monkeypatch):
    async def fake_switch(call_id, seat_id, target_provider, request_id):
        raise RuntimeError("boom")

    class FakeRequest:
        async def json(self):
            return {
                "callId": "agent-stream",
                "model": "xfyun",
                "seatId": "8001",
            }

    monkeypatch.setattr("https_gateway.switch_paired_session_models", fake_switch)
    response = __import__("asyncio").run(asr_model_switch(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 500
    assert_model_switch_envelope(
        body,
        success=False,
        message="INTERNAL_SERVER_ERROR",
        code=500,
        data={"requestId": body["data"]["requestId"]},
    )
