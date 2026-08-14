"""Durable, local audit records for address-scope event processing."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping


class AddressScopeAuditStore:
    """Stores one JSON audit document per event without retaining credentials."""

    def __init__(
        self,
        directory: str | Path | None = None,
        retention_days: int | None = None,
    ) -> None:
        self._directory = Path(directory or os.getenv(
            "ASR_ADDRESS_SCOPE_AUDIT_DIR", "logs/address_scope_audit"
        )).resolve()
        self._retention_days = retention_days if retention_days is not None else int(
            os.getenv("ASR_ADDRESS_SCOPE_AUDIT_RETENTION_DAYS", "7")
        )
        if self._retention_days < 1:
            raise ValueError("address-scope audit retention must be at least one day")
        self._lock = threading.Lock()

    @staticmethod
    def _safe_event_id(event_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)[:160]

    def _path(self, event_id: str) -> Path:
        return self._directory / f"{self._safe_event_id(event_id)}.json"

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")

    def _load(self, event_id: str) -> dict:
        path = self._path(event_id)
        if not path.exists():
            return {"eventId": event_id}
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {"eventId": event_id}

    def _write(self, event_id: str, record: Mapping[str, object]) -> None:
        self._directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        path = self._path(event_id)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
        try:
            path.chmod(0o640)
        except OSError:
            pass

    def _cleanup(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        for path in self._directory.glob("*.json"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                    path.unlink()
            except OSError:
                continue

    def record_received(self, *, event_id: str, payload: Mapping[str, object], dispatch: object) -> None:
        binding = getattr(dispatch, "binding")
        with self._lock:
            record = self._load(event_id)
            record.update({
                "eventId": event_id,
                "updatedAt": self._utc_now(),
                "status": "received",
                "dispatchStatus": getattr(dispatch, "status"),
                "callId": binding.call_id,
                "scopeId": binding.scope_id,
                "inventoryVersion": binding.inventory_version,
                "rawMessage": payload,
            })
            self._write(event_id, record)
            self._cleanup()

    def record_resolved(self, *, event_id: str, result: object) -> None:
        items = list(getattr(result, "items"))
        with self._lock:
            record = self._load(event_id)
            record.update({
                "updatedAt": self._utc_now(),
                "status": "resolved",
                "addressScope": {
                    "itemCount": getattr(result, "item_count"),
                    "poiCount": getattr(result, "poi_count"),
                    "aoiCount": getattr(result, "aoi_count"),
                    "buildingCount": getattr(result, "building_count"),
                    "items": items,
                    "queryMs": getattr(result, "query_ms"),
                },
                "filteredHotwords": list(getattr(result, "filtered_hotwords")),
            })
            self._write(event_id, record)
            self._cleanup()

    def record_applied(self, *, event_id: str, changed: bool) -> None:
        with self._lock:
            record = self._load(event_id)
            record.update({"updatedAt": self._utc_now(), "status": "applied", "hotwordsChanged": changed})
            self._write(event_id, record)

    def record_failed(self, *, event_id: str, stage: str, error: Exception) -> None:
        with self._lock:
            record = self._load(event_id)
            record.update({
                "updatedAt": self._utc_now(),
                "status": "failed",
                "failure": {"stage": stage, "errorType": type(error).__name__},
            })
            self._write(event_id, record)

    def get(self, event_id: str) -> dict | None:
        with self._lock:
            path = self._path(event_id)
            if not path.exists():
                return None
            return self._load(event_id)

    def latest(self) -> dict | None:
        with self._lock:
            try:
                paths = list(self._directory.glob("*.json"))
            except OSError:
                return None
            if not paths:
                return None
            return self._load(max(paths, key=lambda path: path.stat().st_mtime).stem)

    def latest_with_hotwords(self) -> dict | None:
        """Return the newest completed audit that contains address candidates."""
        with self._lock:
            try:
                paths = sorted(
                    self._directory.glob("*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                return None
            for path in paths:
                record = self._load(path.stem)
                if record.get("filteredHotwords"):
                    return record
            return None
