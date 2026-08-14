"""Runtime call control state shared by CTI events and ASR sessions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


HOLD_STARTED_EVENT = "localHoldCall"
HOLD_ENDED_EVENT = "callHoldCancel"


def _string(value: Any) -> str:
    return str(value or "").strip()


def phone_pair(left: Any, right: Any) -> tuple[str, str] | None:
    first = _string(left)
    second = _string(right)
    if not first or not second:
        return None
    return tuple(sorted((first, second)))


@dataclass(frozen=True)
class CallHoldChange:
    event_id: str
    event_type: str
    status: str
    call_id: str
    pair: tuple[str, str] | None
    event_time: str
    ext: dict[str, Any]


class CallHoldState:
    """Track held calls by both CTI callId and phone pair.

    CTI hold events can identify only one side of a two-stream ASR call. The
    phone pair lets us stop the peer stream as well.
    """

    def __init__(self) -> None:
        self._held_call_ids: set[str] = set()
        self._held_pairs: set[tuple[str, str]] = set()
        self._call_id_to_pair: dict[str, tuple[str, str]] = {}
        self._seen_event_ids: set[str] = set()
        self._lock = threading.Lock()

    def apply_cti_event(self, event: dict[str, Any]) -> CallHoldChange | None:
        normalized = self._normalize_event(event)
        if normalized is None:
            return None

        event_id = normalized.event_id
        with self._lock:
            if event_id and event_id in self._seen_event_ids:
                return None
            if event_id:
                self._seen_event_ids.add(event_id)

            if normalized.event_type == HOLD_STARTED_EVENT:
                if normalized.call_id:
                    self._held_call_ids.add(normalized.call_id)
                    if normalized.pair:
                        self._call_id_to_pair[normalized.call_id] = normalized.pair
                if normalized.pair:
                    self._held_pairs.add(normalized.pair)
                return normalized

            if normalized.event_type == HOLD_ENDED_EVENT:
                if normalized.call_id:
                    self._held_call_ids.discard(normalized.call_id)
                    self._call_id_to_pair.pop(normalized.call_id, None)
                if normalized.pair:
                    self._held_pairs.discard(normalized.pair)
                return normalized

        return None

    def is_held(self, *, call_id: Any = "", callfrom: Any = "", callto: Any = "") -> bool:
        call_id_text = _string(call_id)
        pair = phone_pair(callfrom, callto)
        with self._lock:
            if call_id_text and call_id_text in self._held_call_ids:
                return True
            return bool(pair and pair in self._held_pairs)

    def clear_call(self, call_id: Any) -> None:
        call_id_text = _string(call_id)
        if not call_id_text:
            return
        with self._lock:
            self._held_call_ids.discard(call_id_text)
            pair = self._call_id_to_pair.pop(call_id_text, None)
            if pair:
                self._held_pairs.discard(pair)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "heldCallIds": sorted(self._held_call_ids),
                "heldPairs": [list(pair) for pair in sorted(self._held_pairs)],
            }

    def _normalize_event(self, event: dict[str, Any]) -> CallHoldChange | None:
        if not isinstance(event, dict):
            return None

        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if not isinstance(data, dict):
            return None

        event_type = _string(data.get("eventType"))
        if event_type not in {HOLD_STARTED_EVENT, HOLD_ENDED_EVENT}:
            return None

        ext = data.get("ext") if isinstance(data.get("ext"), dict) else {}
        call_id = _string(data.get("callId"))
        event_id = _string(data.get("eventId") or event.get("id"))
        from_number = _string(ext.get("from") or ext.get("callerId"))
        to_number = _string(ext.get("to") or ext.get("extId"))
        status = "holding" if event_type == HOLD_STARTED_EVENT else "active"

        return CallHoldChange(
            event_id=event_id,
            event_type=event_type,
            status=status,
            call_id=call_id,
            pair=phone_pair(from_number, to_number),
            event_time=_string(data.get("eventTime") or event.get("time")),
            ext=dict(ext),
        )
