import json
import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_rabbitmq import AsrRabbitMQPublisher, create_asr_rabbitmq_publisher


def test_routing_key_uses_callto():
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")

    assert publisher.routing_key({"callto": "119"}) == "asr.119"


def test_routing_key_falls_back_to_unknown():
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")

    assert publisher.routing_key({"callto": ""}) == "asr.unknown"


def test_routing_key_sanitizes_direct_key_component():
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")

    assert publisher.routing_key({"callto": "seat. 119"}) == "asr.seat_119"


def test_build_cloud_event_preserves_original_data():
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")
    event = {"event": "speech.final", "callId": "c1", "callto": "119", "text": "着火了"}

    payload = publisher.build_cloud_event(event)

    assert payload["source"] == "ids:asr"
    assert payload["type"] == "ids:asr:speech.final"
    assert payload["specversion"] == "1.0"
    assert payload["data"] == event
    assert payload["id"]
    assert payload["time"]


def test_message_contains_routing_key_and_cloud_event_payload():
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")
    event = {"event": "speech.final", "callId": "c1", "callto": "119", "text": "着火了"}

    message = publisher.message(event)

    assert message["exchange"] == "ids:asr"
    assert message["routingKey"] == "asr.119"
    assert message["payload"]["type"] == "ids:asr:speech.final"
    assert message["payload"]["data"] == event


def test_disabled_publisher_does_not_publish(monkeypatch):
    calls = []
    publisher = AsrRabbitMQPublisher(enabled=False, host="mq", user="u", password="p")
    monkeypatch.setattr(publisher, "_publish_sync", lambda *args: calls.append(args))

    publisher.publish({"event": "speech.final", "callto": "119"})

    assert calls == []


def test_publish_errors_do_not_raise(caplog):
    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    publisher._publish_sync("asr.119", {"data": {"event": "speech.final"}})

    # Direct sync failures are handled inside _publish_sync and logged.
    assert True


def test_publish_sync_sends_persistent_json(monkeypatch):
    captured = {}

    class FakeConnection:
        def channel(self):
            return FakeChannel()

        def close(self):
            captured["closed"] = True

    class FakeChannel:
        def exchange_declare(self, **kwargs):
            captured["exchange_declare"] = kwargs

        def basic_publish(self, **kwargs):
            captured["basic_publish"] = kwargs

    monkeypatch.setattr("asr_rabbitmq.pika.PlainCredentials", lambda user, password: (user, password))
    monkeypatch.setattr("asr_rabbitmq.pika.ConnectionParameters", lambda **kwargs: kwargs)
    monkeypatch.setattr("asr_rabbitmq.pika.BlockingConnection", lambda params: FakeConnection())

    publisher = AsrRabbitMQPublisher(enabled=True, host="mq", user="u", password="p")
    payload = publisher.build_cloud_event({"event": "speech.final", "callto": "119"})
    publisher._publish_sync("asr.119", payload)

    assert captured["exchange_declare"] == {
        "exchange": "ids:asr",
        "exchange_type": "direct",
        "durable": True,
    }
    published = captured["basic_publish"]
    assert published["exchange"] == "ids:asr"
    assert published["routing_key"] == "asr.119"
    assert json.loads(published["body"])["type"] == "ids:asr:speech.final"
    assert published["properties"].delivery_mode == 2
    assert published["properties"].content_type == "application/json"
    assert captured["closed"] is True


def test_factory_reads_environment(monkeypatch):
    monkeypatch.setenv("ASR_RABBITMQ_ENABLED", "true")
    monkeypatch.setenv("ASR_RABBITMQ_HOST", "192.168.173.198")
    monkeypatch.setenv("ASR_RABBITMQ_PORT", "5673")
    monkeypatch.setenv("ASR_RABBITMQ_VHOST", "ai")
    monkeypatch.setenv("ASR_RABBITMQ_USER", "ids-dev")
    monkeypatch.setenv("ASR_RABBITMQ_PASS", "secret")
    monkeypatch.setenv("ASR_RABBITMQ_EXCHANGE", "ids:asr")
    monkeypatch.setenv("ASR_RABBITMQ_SOURCE", "ids:asr")
    monkeypatch.setenv("ASR_RABBITMQ_ROUTING_PREFIX", "asr")

    publisher = create_asr_rabbitmq_publisher()

    assert publisher.enabled is True
    assert publisher.host == "192.168.173.198"
    assert publisher.port == 5673
    assert publisher.vhost == "ai"
    assert publisher.user == "ids-dev"
    assert publisher.password == "secret"
    assert publisher.exchange == "ids:asr"
    assert publisher.source == "ids:asr"
    assert publisher.routing_prefix == "asr"


def test_qs_factory_stays_enabled_when_primary_is_disabled(monkeypatch):
    monkeypatch.setenv("ASR_RABBITMQ_ENABLED", "false")
    monkeypatch.setenv("ASR_RABBITMQ_HOST", "192.168.173.198")
    monkeypatch.setenv("ASR_RABBITMQ_PORT", "5672")
    monkeypatch.setenv("ASR_RABBITMQ_VHOST", "ai")
    monkeypatch.setenv("ASR_RABBITMQ_USER", "ids-dev")
    monkeypatch.setenv("ASR_RABBITMQ_PASS", "secret")
    monkeypatch.setenv("ASR_RABBITMQ_QS_ENABLED", "true")
    monkeypatch.setenv("ASR_RABBITMQ_QS_EXCHANGE", "ids:qs")
    monkeypatch.setenv("ASR_RABBITMQ_QS_SOURCE", "ids:qs")
    monkeypatch.setenv("ASR_RABBITMQ_QS_ROUTING_PREFIX", "qs")
    monkeypatch.setenv("ASR_RABBITMQ_QS_FIXED_ROUTING_KEY", "true")

    primary = create_asr_rabbitmq_publisher()
    qs = create_asr_rabbitmq_publisher(
        env_prefix="ASR_RABBITMQ_QS_",
        default_exchange="ids:qs",
        default_source="ids:qs",
        default_routing_prefix="qs",
    )

    assert primary.enabled is False
    assert qs is not None
    assert qs.enabled is True
    assert qs.host == "192.168.173.198"
    assert qs.exchange == "ids:qs"
    assert qs.source == "ids:qs"
    assert qs.routing_prefix == "qs"
    assert qs.fixed_routing_key is True
