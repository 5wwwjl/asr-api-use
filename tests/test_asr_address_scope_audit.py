from pathlib import Path

from asr_address_scope_audit import AddressScopeAuditStore


class Binding:
    call_id = "call-1"
    scope_id = "scope-1"
    inventory_version = "v1"


class Dispatch:
    binding = Binding()
    status = "bound_active"


class Result:
    item_count = 1
    poi_count = 1
    aoi_count = 0
    building_count = 0
    query_ms = 2.5
    items = ({"inventoryId": "poi-1", "sourceType": "POI", "standardName": "会议中心"},)
    filtered_hotwords = ({"word": "会议中心", "weight": 20, "sourceField": "standardName"},)


def test_audit_store_links_raw_items_and_filtered_result(tmp_path: Path):
    store = AddressScopeAuditStore(tmp_path, retention_days=7)
    payload = {"id": "event-1", "data": {"sessionId": "call-1"}}

    store.record_received(event_id="event-1", payload=payload, dispatch=Dispatch())
    store.record_resolved(event_id="event-1", result=Result())
    store.record_applied(event_id="event-1", changed=True)

    record = store.latest()
    assert record["rawMessage"] == payload
    assert record["addressScope"]["items"] == list(Result.items)
    assert record["filteredHotwords"] == list(Result.filtered_hotwords)
    assert record["status"] == "applied"
    assert store.get("event-1") == record


def test_latest_with_hotwords_skips_newer_received_only_event(tmp_path: Path):
    store = AddressScopeAuditStore(tmp_path, retention_days=7)
    store.record_received(
        event_id="completed-event",
        payload={"id": "completed-event"},
        dispatch=Dispatch(),
    )
    store.record_resolved(event_id="completed-event", result=Result())
    store.record_received(
        event_id="received-only-event",
        payload={"id": "received-only-event"},
        dispatch=Dispatch(),
    )

    assert store.latest()["eventId"] == "received-only-event"
    assert store.latest_with_hotwords()["eventId"] == "completed-event"
