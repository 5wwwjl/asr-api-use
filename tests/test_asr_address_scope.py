import asyncio
import json
import threading
from uuid import uuid4

import pytest

import asr_bridge
from asr_address_scope import (
    AddressScopeBindingStore,
    InvalidAddressScopeEvent,
    parse_address_scope_event,
)
from asr_address_scope_rabbitmq import (
    AddressScopeRabbitMQConfig,
    AddressScopeRabbitMQConsumer,
)
from asr_address_scope_client import AddressHotwordResult, AddressScopeClient
from hotword_manager import HotwordManager


def event(*, event_id=None, call_id="call-address-1", scope_id=None):
    event_id = event_id or str(uuid4())
    scope_id = scope_id or str(uuid4())
    location_id = str(uuid4())
    return {
        "id": event_id,
        "specversion": "1.0",
        "type": "address.scope.ready.v1",
        "subject": scope_id,
        "data": {
            "eventId": event_id,
            "eventType": "address.scope.ready.v1",
            "schemaVersion": "1.0",
            "occurredAt": "2026-08-05T14:00:00+08:00",
            "sessionId": call_id,
            "addressScopeRef": {
                "scopeId": scope_id,
                "locationResolutionId": location_id,
                "locationResolutionVersion": 1,
                "inventoryVersion": "REALISTIC_AI_SOURCE_V1",
            },
            "itemsPath": f"/api/v1/address-scopes/{scope_id}/items",
        },
    }


def test_scope_event_contract_and_pending_deduplication():
    now = [0.0]
    store = AddressScopeBindingStore(ttl_seconds=10, max_entries=2, clock=lambda: now[0])
    payload = event()

    first = store.accept(payload, call_active=False)
    duplicate = store.accept(payload, call_active=False)

    assert first.status == "pending_session"
    assert duplicate.status == "duplicate"
    assert store.take_pending(first.binding.call_id) == first.binding


def test_scope_store_expires_pending_records():
    now = [0.0]
    store = AddressScopeBindingStore(ttl_seconds=5, max_entries=2, clock=lambda: now[0])
    binding = store.accept(event(), call_active=False).binding
    now[0] = 5.0

    assert store.purge_expired() == 1
    assert store.take_pending(binding.call_id) is None


def test_scope_event_rejects_mismatched_subject():
    payload = event()
    payload["subject"] = str(uuid4())

    with pytest.raises(InvalidAddressScopeEvent, match="subject"):
        parse_address_scope_event(payload)


def test_bridge_binds_pending_scope_when_session_registers():
    class FakeSession:
        call_id = "call-address-pending"
        call_ended = False
        binding = None

        def set_address_scope_binding(self, value):
            self.binding = value

        def _track_background_task(self, task, _label):
            task.cancel()

    async def scenario():
        payload = event(call_id=FakeSession.call_id)
        queued = await asr_bridge.accept_address_scope_event(payload)
        assert queued.status == "pending_session"
        session = FakeSession()
        await asr_bridge.register_session(session)
        try:
            assert session.binding is not None
            assert session.binding.scope_id == queued.binding.scope_id
        finally:
            await asr_bridge.unregister_session(session.call_id, session)

    asyncio.run(scenario())


def test_consumer_acks_valid_handler_result():
    class FakeChannel:
        acks = []
        nacks = []

        def basic_ack(self, **kwargs):
            self.acks.append(kwargs)

        def basic_nack(self, **kwargs):
            self.nacks.append(kwargs)

    class Method:
        delivery_tag = 7

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever)
    loop_thread.start()

    async def handler(payload):
        assert payload["type"] == "address.scope.ready.v1"

    consumer = AddressScopeRabbitMQConsumer(
        AddressScopeRabbitMQConfig(enabled=True, host="localhost", user="u", password="p"),
        loop=loop,
        handler=handler,
    )
    channel = FakeChannel()
    try:
        consumer._on_message(channel, Method(), None, json.dumps(event()).encode())
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join()
        loop.close()

    assert channel.acks == [{"delivery_tag": 7}]
    assert channel.nacks == []


def test_consumer_rejects_invalid_json_without_requeue():
    class FakeChannel:
        nacks = []

        def basic_ack(self, **kwargs):
            raise AssertionError("invalid messages must not ACK")

        def basic_nack(self, **kwargs):
            self.nacks.append(kwargs)

    class Method:
        delivery_tag = 8

    loop = asyncio.new_event_loop()
    consumer = AddressScopeRabbitMQConsumer(
        AddressScopeRabbitMQConfig(enabled=True, host="localhost", user="u", password="p"),
        loop=loop,
        handler=lambda _payload: asyncio.sleep(0),
    )
    channel = FakeChannel()

    consumer._on_message(channel, Method(), None, b"not-json")

    assert channel.nacks == [{"delivery_tag": 8, "requeue": False}]


def test_address_extraction_uses_only_name_fields():
    words, counts, filtered = AddressScopeClient._extract([
        {
            "inventoryId": "poi-1",
            "sourceType": "POI",
            "standardName": "国际会议中心",
            "shortName": "会议中心",
            "aoiName": "会展城",
            "aliases": ["会议中心站", "12345"],
            "fullAddress": "不应作为热词的完整地址",
            "longitude": 116.1,
        },
        {
            "inventoryId": "building-1",
            "sourceType": "BUILDING",
            "standardName": "会展城一号楼",
            "aoiName": "会展城",
        },
    ])

    assert words == {
        "会展城": 20,
        "会展城一号楼": 20,
        "国际会议中心": 20,
        "会议中心": 15,
        "会议中心站": 15,
    }
    assert counts == {"POI": 1, "AOI": 1, "BUILDING": 1, "alias": 2}
    assert {item["word"]: item["sourceField"] for item in filtered} == {
        "国际会议中心": "standardName",
        "会展城": "aoiName",
        "会展城一号楼": "standardName",
        "会议中心": "shortName",
        "会议中心站": "aliases",
    }


def test_database_source_requires_read_only_connection_configuration(monkeypatch):
    monkeypatch.setenv("ASR_ADDRESS_SCOPE_DB_HOST", "db")
    monkeypatch.setenv("ASR_ADDRESS_SCOPE_DB_USER", "reader")
    monkeypatch.setenv("ASR_ADDRESS_SCOPE_DB_PASSWORD", "secret")

    client = AddressScopeClient(source="database")

    assert client._source == "database"
    assert client._db_host == "db"


def test_address_hotwords_replace_scope_and_apply_to_scene_snapshot():
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir="/home/twai/wjl/DynamicHotwordLoading/hotwords",
    )

    assert manager.set_address_hotwords(
        scope_id="scope-a",
        inventory_version="inventory-v1",
        hotwords={"国际会议中心": 20, "会议中心": 15},
    )
    assert "国际会议中心" in manager.current_hotwords().split()
    assert "address.scope:scope-a" in manager.library_ids

    assert manager.set_address_hotwords(
        scope_id="scope-b",
        inventory_version="inventory-v1",
        hotwords={"会展城一号楼": 20},
    )
    assert "国际会议中心" not in manager.current_hotwords().split()
    assert "会展城一号楼" in manager.current_hotwords().split()
    assert "address.scope:scope-b" in manager.library_ids


def test_active_bridge_scope_event_queries_and_queues_hotwords(monkeypatch):
    class FakeAudit:
        def record_received(self, **_kwargs):
            pass

        def record_resolved(self, **_kwargs):
            pass

        def record_applied(self, **_kwargs):
            pass

        def record_failed(self, **_kwargs):
            pass

    class FakeSession:
        call_id = "call-address-active"
        call_ended = False
        binding = None
        hotwords = None

        def set_address_scope_binding(self, value):
            self.binding = value

        def set_address_scope_hotwords(self, binding, hotwords):
            self.binding = binding
            self.hotwords = hotwords
            return True

    class FakeClient:
        async def fetch_hotwords(self, binding):
            return AddressHotwordResult(
                hotwords={"国际会议中心": 20},
                items=(),
                filtered_hotwords=(),
                item_count=1,
                poi_count=1,
                aoi_count=0,
                building_count=0,
                alias_count=0,
                query_ms=1.0,
            )

    monkeypatch.setattr(asr_bridge, "_address_scope_client", FakeClient())
    monkeypatch.setattr(asr_bridge, "_address_scope_audit", FakeAudit())

    async def scenario():
        session = FakeSession()
        await asr_bridge.register_session(session)
        try:
            dispatched = await asr_bridge.accept_address_scope_event(
                event(call_id=session.call_id)
            )
            assert dispatched.status == "bound_active"
            assert session.hotwords == {"国际会议中心": 20}
        finally:
            await asr_bridge.unregister_session(session.call_id, session)

    asyncio.run(scenario())
