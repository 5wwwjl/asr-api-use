import asyncio
import json
import sys
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_message_service import (  # noqa: E402
    AsrMessageServicePublisher,
    MessageServiceConfig,
)


def message_config(**overrides):
    values = {
        "enabled": True,
        "uac_base_url": "http://ids-dev.ks.telewave.tech/uac-client",
        "client_id": "ids-asr-service",
        "client_secret": "test-client-secret",
        "send_url": "http://ids-dev.ks.telewave.tech/message-client/messages/send",
        "tenant_id": "tenant-1",
        "retry_delay_seconds": 0,
    }
    values.update(overrides)
    return MessageServiceConfig(**values)


def speech_event(**overrides):
    event = {
        "eventId": "event-1",
        "event": "speech.final",
        "callId": "call-1",
        "callfrom": "13800000000",
        "callto": "119",
        "text": "科兴科学园发生火灾",
    }
    event.update(overrides)
    return event


def test_request_body_targets_seat_and_nests_cloud_event_in_custom_content():
    publisher = AsrMessageServicePublisher(message_config())
    event = speech_event()

    cloud_event = publisher.build_cloud_event(event)
    body = publisher.build_request_body(event, cloud_event=cloud_event)

    assert "customContent" not in body
    message = body["userMsgBodyList"][0]
    assert message["content"] == "ASR实时语音转写"
    assert message["topics"] == [{"type": "SEAT", "key": "119"}]
    assert message["clientDto"] == {"code": "ids-seat-web", "desc": "坐席端"}
    assert message["notifyTypeDto"] == {
        "notifyType": "asr",
        "notifySubType": "speech.final",
    }
    assert message["request"] == {
        "channel": "WEBSOCKET",
        "customContent": cloud_event,
    }
    assert cloud_event["id"] == "event-1"
    assert cloud_event["source"] == "ids:asr"
    assert cloud_event["type"] == "ids:asr:speech.final"
    assert cloud_event["specversion"] == "1.0"
    assert cloud_event["data"] == event
    assert cloud_event["data"]["text"] == "科兴科学园发生火灾"


def test_request_body_requires_callto():
    publisher = AsrMessageServicePublisher(message_config())

    try:
        publisher.build_request_body(speech_event(callto=""))
    except ValueError as exc:
        assert "callto" in str(exc)
    else:
        raise AssertionError("empty callto must be rejected")


def test_gateway_ip_rewrites_connection_url_and_preserves_host_header():
    config = message_config(
        gateway_ip="192.168.169.252",
        host_header="ids-dev.ks.telewave.tech",
    )

    assert config.request_url(config.send_url) == (
        "http://192.168.169.252/message-client/messages/send"
    )
    assert config.gateway_headers(config.send_url) == {
        "Host": "ids-dev.ks.telewave.tech"
    }


def test_queue_drops_oldest_and_rejects_empty_callto():
    publisher = AsrMessageServicePublisher(message_config(queue_size=1))
    publisher._started = True

    assert publisher.enqueue(speech_event(eventId="event-1")) is True
    assert publisher.enqueue(speech_event(eventId="event-2")) is True
    assert publisher.dropped_count == 1
    assert publisher._queue.get_nowait()["eventId"] == "event-2"
    publisher._queue.task_done()
    assert publisher.enqueue(speech_event(callto="")) is False


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return json.dumps(self.payload, ensure_ascii=False)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": dict(headers), "json": json})
        return self.responses.pop(0)

    async def close(self):
        return None


def test_auth_error_refreshes_token_once_and_retries_message():
    async def exercise():
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "code": 200,
                        "data": {"access_token": "token-1", "expires_in": 300},
                    },
                ),
                FakeResponse(412, {"code": 41202, "message": "token expired"}),
                FakeResponse(
                    200,
                    {
                        "code": 200,
                        "data": {"access_token": "token-2", "expires_in": 300},
                    },
                ),
                FakeResponse(200, {"code": 200, "message": "接收消息成功"}),
            ]
        )
        publisher = AsrMessageServicePublisher(message_config())
        publisher._session = session

        assert await publisher._deliver(speech_event()) is True
        assert publisher.sent_count == 1
        assert publisher.failed_count == 0

        login_calls = [
            call for call in session.calls
            if call["url"].endswith("/loginByClientCredentials")
        ]
        send_calls = [
            call for call in session.calls
            if call["url"].endswith("/message-client/messages/send")
        ]
        assert len(login_calls) == 2
        assert len(send_calls) == 2
        assert [call["headers"]["Authorization"] for call in send_calls] == [
            "Bearer token-1",
            "Bearer token-2",
        ]
        assert all(call["headers"]["clientId"] == "ids-asr-service" for call in send_calls)
        assert all(call["headers"]["tenantId"] == "tenant-1" for call in send_calls)

    asyncio.run(exercise())


def test_missing_required_config_prevents_start():
    config = MessageServiceConfig(enabled=True, client_id="ids-asr-service")
    publisher = AsrMessageServicePublisher(config)

    assert asyncio.run(publisher.start()) is False
    assert publisher.started is False
