"""Address-scope CloudEvent validation and call-level pending bindings."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Mapping
from uuid import UUID


ADDRESS_SCOPE_EVENT_TYPE = "address.scope.ready.v1"


class InvalidAddressScopeEvent(ValueError):
    """The message is not a valid address-scope CloudEvent."""


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidAddressScopeEvent(f"{field} must be a non-empty string")
    return value.strip()


def _required_uuid(value: object, field: str) -> str:
    text = _required_string(value, field)
    try:
        UUID(text)
    except ValueError as exc:
        raise InvalidAddressScopeEvent(f"{field} must be a UUID") from exc
    return text


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidAddressScopeEvent(f"{field} must be an object")
    return value


@dataclass(frozen=True)
class AddressScopeBinding:
    event_id: str
    call_id: str
    scope_id: str
    location_resolution_id: str
    location_resolution_version: int
    inventory_version: str
    items_path: str
    occurred_at: str


@dataclass(frozen=True)
class AddressScopeDispatch:
    binding: AddressScopeBinding
    status: str

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"


def parse_address_scope_event(payload: Mapping[str, object]) -> AddressScopeBinding:
    if payload.get("type") != ADDRESS_SCOPE_EVENT_TYPE:
        raise InvalidAddressScopeEvent(f"type must be {ADDRESS_SCOPE_EVENT_TYPE}")
    if payload.get("specversion") != "1.0":
        raise InvalidAddressScopeEvent("specversion must be 1.0")

    event_id = _required_uuid(payload.get("id"), "id")
    subject = _required_uuid(payload.get("subject"), "subject")
    data = _mapping(payload.get("data"), "data")
    if data.get("eventType") != ADDRESS_SCOPE_EVENT_TYPE:
        raise InvalidAddressScopeEvent(f"data.eventType must be {ADDRESS_SCOPE_EVENT_TYPE}")
    if data.get("schemaVersion") != "1.0":
        raise InvalidAddressScopeEvent("data.schemaVersion must be 1.0")
    if _required_uuid(data.get("eventId"), "data.eventId") != event_id:
        raise InvalidAddressScopeEvent("id must equal data.eventId")

    ref = _mapping(data.get("addressScopeRef"), "data.addressScopeRef")
    scope_id = _required_uuid(ref.get("scopeId"), "data.addressScopeRef.scopeId")
    if subject != scope_id:
        raise InvalidAddressScopeEvent("subject must equal data.addressScopeRef.scopeId")
    location_version = ref.get("locationResolutionVersion")
    if type(location_version) is not int or location_version < 1:
        raise InvalidAddressScopeEvent("locationResolutionVersion must be a positive integer")
    items_path = _required_string(data.get("itemsPath"), "data.itemsPath")
    expected_path = f"/api/v1/address-scopes/{scope_id}/items"
    if items_path != expected_path:
        raise InvalidAddressScopeEvent(f"data.itemsPath must be {expected_path}")

    return AddressScopeBinding(
        event_id=event_id,
        call_id=_required_string(data.get("sessionId"), "data.sessionId"),
        scope_id=scope_id,
        location_resolution_id=_required_uuid(
            ref.get("locationResolutionId"),
            "data.addressScopeRef.locationResolutionId",
        ),
        location_resolution_version=location_version,
        inventory_version=_required_string(
            ref.get("inventoryVersion"), "data.addressScopeRef.inventoryVersion"
        ),
        items_path=items_path,
        occurred_at=_required_string(data.get("occurredAt"), "data.occurredAt"),
    )


class AddressScopeBindingStore:
    """Deduplicates events and holds scopes that arrive before their ASR call."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 1800,
        max_entries: int = 10000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("address-scope store limits must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._seen_events: dict[str, float] = {}
        self._pending: dict[str, tuple[AddressScopeBinding, float]] = {}

    def accept(self, payload: Mapping[str, object], *, call_active: bool) -> AddressScopeDispatch:
        self.purge_expired()
        binding = parse_address_scope_event(payload)
        if binding.event_id in self._seen_events:
            return AddressScopeDispatch(binding=binding, status="duplicate")

        now = self._clock()
        self._seen_events[binding.event_id] = now
        self._trim_seen_events()
        if call_active:
            return AddressScopeDispatch(binding=binding, status="bound_active")

        self._pending[binding.call_id] = (binding, now)
        self._trim_pending()
        return AddressScopeDispatch(binding=binding, status="pending_session")

    def take_pending(self, call_id: str) -> AddressScopeBinding | None:
        self.purge_expired()
        pending = self._pending.pop(str(call_id or "").strip(), None)
        return pending[0] if pending else None

    def forget_event(self, event_id: str) -> None:
        """Allow a transient downstream failure to be retried by RabbitMQ."""
        self._seen_events.pop(event_id, None)

    def purge_expired(self) -> int:
        cutoff = self._clock() - self._ttl_seconds
        expired_pending = [key for key, (_, added_at) in self._pending.items() if added_at <= cutoff]
        for key in expired_pending:
            self._pending.pop(key, None)
        self._seen_events = {
            event_id: seen_at
            for event_id, seen_at in self._seen_events.items()
            if seen_at > cutoff
        }
        return len(expired_pending)

    @property
    def pending_count(self) -> int:
        self.purge_expired()
        return len(self._pending)

    def _trim_seen_events(self) -> None:
        overflow = len(self._seen_events) - self._max_entries
        if overflow > 0:
            for event_id, _ in sorted(self._seen_events.items(), key=lambda item: item[1])[:overflow]:
                self._seen_events.pop(event_id, None)

    def _trim_pending(self) -> None:
        overflow = len(self._pending) - self._max_entries
        if overflow > 0:
            for call_id, _ in sorted(self._pending.items(), key=lambda item: item[1][1])[:overflow]:
                self._pending.pop(call_id, None)
