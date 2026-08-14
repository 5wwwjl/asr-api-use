"""State and FunASR helpers for the executive dynamic-hotword A/B demo."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Mapping
from uuid import UUID
import wave

import aiohttp

from asr_address_scope import AddressScopeBinding
from asr_address_scope_client import AddressScopeClient, AddressScopeQueryError
from hotword_manager import HotwordManager, InvalidSceneSignal


LIBRARY_LABELS = {
    "baseline": "基础热词库",
    "classification_assist.call_type": "来电分类辅助热词库",
    "call_type.fire_fighting": "火灾扑救热词库",
    "call_type.social_assistance": "社会救助热词库",
    "call_type.emergency_rescue": "抢险救援热词库",
    "building_usage.medical_eldercare": "医疗养老场所热词库",
    "building_usage.crowded_place": "人员密集场所热词库",
    "building_usage.industrial_storage_site": "工业仓储场所热词库",
    "building_usage.residential_commercial_hotel": "住宅商业酒店热词库",
    "building_usage.education_training": "教育培训场所热词库",
    "building_structure.highrise_multistory": "高层多层建筑热词库",
    "building_structure.underground": "地下空间热词库",
    "building_structure.open_air": "露天区域热词库",
}

ADDRESS_DEMO_EXAMPLES = (
    (
        "东方科技大厦",
        "我在东方科技大厦，电缆井冒烟，烟气正在竖向蔓延。",
    ),
    (
        "万基产业园2栋",
        "我在万基产业园二栋，转换层旁边的避难间已经进烟了。",
    ),
    (
        "中国储能大厦",
        "我在中国储能大厦地下停车场，配电间正在冒烟。",
    ),
)


class RealLocationDemoError(RuntimeError):
    """The real location demo request or address lookup could not complete."""


@dataclass(frozen=True)
class RealLocationDemoConfig:
    base_url: str
    longitude: float
    latitude: float
    radius_meters: int
    accuracy_meters: int
    expected_inventory_version: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "RealLocationDemoConfig":
        return cls(
            base_url=os.getenv(
                "HOTWORD_DEMO_LOCATION_BASE_URL",
                "http://192.168.173.167:18082",
            ).rstrip("/"),
            longitude=float(os.getenv("HOTWORD_DEMO_LOCATION_LONGITUDE", "113.93924230")),
            latitude=float(os.getenv("HOTWORD_DEMO_LOCATION_LATITUDE", "22.55250952")),
            radius_meters=int(os.getenv("HOTWORD_DEMO_LOCATION_RADIUS_METERS", "2000")),
            accuracy_meters=int(os.getenv("HOTWORD_DEMO_LOCATION_ACCURACY_METERS", "30")),
            expected_inventory_version=os.getenv(
                "HOTWORD_DEMO_LOCATION_INVENTORY_VERSION",
                "ODS7ALM_AI_REAL_20260810_V1",
            ).strip(),
            timeout_seconds=float(os.getenv("HOTWORD_DEMO_LOCATION_TIMEOUT_SECONDS", "10")),
        )

    def validate(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("real location base URL must use HTTP or HTTPS")
        if not -180 <= self.longitude <= 180 or not -90 <= self.latitude <= 90:
            raise ValueError("real location coordinates are invalid")
        if self.radius_meters <= 0 or self.accuracy_meters <= 0 or self.timeout_seconds <= 0:
            raise ValueError("real location numeric settings must be positive")
        if not self.expected_inventory_version:
            raise ValueError("real location inventory version is required")


class RealLocationDemoClient:
    """Resolve the fixed real coordinate and read its address hotword scope."""

    def __init__(
        self,
        *,
        config: RealLocationDemoConfig | None = None,
        scope_client: AddressScopeClient | None = None,
    ) -> None:
        self._config = config or RealLocationDemoConfig.from_env()
        self._config.validate()
        self._scope_client = scope_client or AddressScopeClient(
            base_url=self._config.base_url
        )

    @property
    def config(self) -> RealLocationDemoConfig:
        return self._config

    def build_request(self, *, call_id: str, request_id: str) -> dict[str, object]:
        captured_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        return {
            "requestId": request_id,
            "sessionId": call_id,
            "alarmId": call_id,
            "sourceType": "CTI_COORDINATE",
            "baseStationCoordinate": {
                "longitude": self._config.longitude,
                "latitude": self._config.latitude,
                "coordinateSystem": "WGS84",
                "accuracyMeters": self._config.accuracy_meters,
                "capturedAt": captured_at,
            },
            "radiusMeters": self._config.radius_meters,
        }

    def parse_response(
        self,
        payload: Mapping[str, object],
        *,
        call_id: str,
        event_id: str,
    ) -> AddressScopeBinding:
        if payload.get("success") is not True or payload.get("code") != "OK":
            raise RealLocationDemoError("REAL_LOCATION_RESPONSE_NOT_OK")
        data = payload.get("data")
        if not isinstance(data, Mapping) or data.get("sessionId") != call_id:
            raise RealLocationDemoError("REAL_LOCATION_SESSION_MISMATCH")
        ref = data.get("addressScopeRef")
        if not isinstance(ref, Mapping) or ref.get("scopeStatus") != "READY":
            raise RealLocationDemoError("REAL_ADDRESS_SCOPE_NOT_READY")

        scope_id = str(ref.get("scopeId") or "").strip()
        resolution_id = str(ref.get("locationResolutionId") or "").strip()
        try:
            UUID(scope_id)
            UUID(resolution_id)
        except ValueError as exc:
            raise RealLocationDemoError("REAL_LOCATION_INVALID_IDENTIFIER") from exc
        location_version = ref.get("locationResolutionVersion")
        if type(location_version) is not int or location_version < 1:
            raise RealLocationDemoError("REAL_LOCATION_INVALID_VERSION")
        inventory_version = str(ref.get("inventoryVersion") or "").strip()
        if inventory_version != self._config.expected_inventory_version:
            raise RealLocationDemoError("REAL_LOCATION_INVENTORY_MISMATCH")

        return AddressScopeBinding(
            event_id=event_id,
            call_id=call_id,
            scope_id=scope_id,
            location_resolution_id=resolution_id,
            location_resolution_version=location_version,
            inventory_version=inventory_version,
            items_path=f"/api/v1/address-scopes/{scope_id}/items",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

    async def resolve(
        self,
        session: aiohttp.ClientSession,
        *,
        call_id: str,
    ) -> dict[str, object]:
        call_id = str(call_id or "").strip()
        if not call_id:
            raise RealLocationDemoError("REAL_LOCATION_CALL_ID_REQUIRED")
        request_id = f"hotword-demo-real-{uuid.uuid4().hex}"
        event_id = str(uuid.uuid4())
        request_payload = self.build_request(call_id=call_id, request_id=request_id)
        started = time.perf_counter()
        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
        try:
            async with session.post(
                f"{self._config.base_url}/api/v1/location/resolutions",
                json=request_payload,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    raise RealLocationDemoError(f"REAL_LOCATION_HTTP_{response.status}")
                response_payload = await response.json()
        except RealLocationDemoError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RealLocationDemoError("REAL_LOCATION_REQUEST_FAILED") from exc
        if not isinstance(response_payload, Mapping):
            raise RealLocationDemoError("REAL_LOCATION_INVALID_RESPONSE")
        resolution_ms = round((time.perf_counter() - started) * 1000, 3)
        binding = self.parse_response(
            response_payload,
            call_id=call_id,
            event_id=event_id,
        )
        try:
            result = await self._scope_client.fetch_hotwords(binding)
        except AddressScopeQueryError as exc:
            raise RealLocationDemoError("REAL_ADDRESS_SCOPE_QUERY_FAILED") from exc
        return {
            "eventId": event_id,
            "callId": call_id,
            "scopeId": binding.scope_id,
            "inventoryVersion": binding.inventory_version,
            "addressScope": {
                "itemCount": result.item_count,
                "poiCount": result.poi_count,
                "aoiCount": result.aoi_count,
                "buildingCount": result.building_count,
                "queryMs": result.query_ms,
            },
            "filteredHotwords": list(result.filtered_hotwords),
            "location": {
                "environment": "real-167",
                "longitude": self._config.longitude,
                "latitude": self._config.latitude,
                "radiusMeters": self._config.radius_meters,
                "resolutionMs": resolution_ms,
            },
        }


@dataclass
class DemoSession:
    manager: HotwordManager
    created_at: float
    address_summary: dict | None = None


class HotwordDemoService:
    """Keeps small, isolated hotword snapshots for browser demo sessions."""

    def __init__(
        self,
        *,
        scene_hotword_dir: str | Path = "/home/twai/wjl/DynamicHotwordLoading/hotwords",
        max_sessions: int = 20,
        session_ttl_seconds: float = 3600,
    ) -> None:
        self._scene_hotword_dir = Path(scene_hotword_dir)
        self._max_sessions = max_sessions
        self._session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, DemoSession] = {}

    def start_session(self) -> dict:
        started = time.perf_counter()
        manager = HotwordManager(
            mode="scene_dynamic",
            scene_hotword_dir=self._scene_hotword_dir,
        )
        session_id = f"demo-{uuid.uuid4().hex[:12]}"
        self._sessions[session_id] = DemoSession(
            manager=manager,
            created_at=time.monotonic(),
        )
        self._purge_sessions()
        result = self._snapshot(session_id)
        result.update({
            "stage": "preloaded",
            "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
        })
        return result

    def apply_address_audit(self, session_id: str, audit: Mapping[str, object]) -> dict:
        session = self._require_session(session_id)
        started = time.perf_counter()
        raw_hotwords = audit.get("filteredHotwords")
        if not isinstance(raw_hotwords, list) or not raw_hotwords:
            raise ValueError("address audit has no filtered hotwords")

        address_hotwords: dict[str, int] = {}
        for item in raw_hotwords:
            if not isinstance(item, Mapping):
                continue
            word = str(item.get("word") or "").strip()
            weight = item.get("weight")
            if not word or type(weight) is not int or weight <= 0:
                continue
            address_hotwords[word] = max(weight, address_hotwords.get(word, 0))
        if not address_hotwords:
            raise ValueError("address audit has no usable hotwords")

        selected_address_words = list(address_hotwords)
        if session.manager.max_address_hotwords > 0:
            selected_address_words = selected_address_words[
                : session.manager.max_address_hotwords
            ]
        selected_address_words_set = set(selected_address_words)
        examples = [
            {"hotword": hotword, "text": text}
            for hotword, text in ADDRESS_DEMO_EXAMPLES
            if hotword in selected_address_words_set
        ]

        scope_id = str(audit.get("scopeId") or "").strip()
        inventory_version = str(audit.get("inventoryVersion") or "").strip()
        if not scope_id or not inventory_version:
            raise ValueError("address audit is missing scope metadata")

        changed = session.manager.set_address_hotwords(
            scope_id=scope_id,
            inventory_version=inventory_version,
            hotwords=address_hotwords,
        )
        address_scope = audit.get("addressScope")
        address_scope = address_scope if isinstance(address_scope, Mapping) else {}
        location = audit.get("location")
        location = location if isinstance(location, Mapping) else {}
        summary = {
            "eventId": str(audit.get("eventId") or ""),
            "callId": str(audit.get("callId") or ""),
            "scopeId": scope_id,
            "inventoryVersion": inventory_version,
            "candidateCount": len(raw_hotwords),
            "uniqueCandidateCount": len(address_hotwords),
            "selectedAddressCount": session.manager.address_hotword_count,
            "itemCount": int(address_scope.get("itemCount") or 0),
            "poiCount": int(address_scope.get("poiCount") or 0),
            "aoiCount": int(address_scope.get("aoiCount") or 0),
            "buildingCount": int(address_scope.get("buildingCount") or 0),
            "queryMs": round(float(address_scope.get("queryMs") or 0), 3),
            "processingMs": round((time.perf_counter() - started) * 1000, 3),
            "environment": str(location.get("environment") or "unknown"),
            "longitude": float(location.get("longitude") or 0),
            "latitude": float(location.get("latitude") or 0),
            "radiusMeters": int(location.get("radiusMeters") or 0),
            "resolutionMs": round(float(location.get("resolutionMs") or 0), 3),
            "examples": examples,
            "changed": changed,
        }
        session.address_summary = summary
        result = self._snapshot(session_id)
        result.update({"stage": "address_ready", "address": summary})
        return result

    def apply_scene_signals(
        self,
        session_id: str,
        signals: Mapping[str, object],
    ) -> dict:
        session = self._require_session(session_id)
        started = time.perf_counter()
        changed = session.manager.apply_event({
            "eventType": "scene_signal.add",
            "signals": dict(signals),
        })
        result = self._snapshot(session_id)
        result.update({
            "stage": "scene_ready",
            "changed": changed,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
            "effectiveFrom": "next_segment",
        })
        return result

    def hotwords(self, session_id: str) -> str:
        return self._require_session(session_id).manager.current_hotwords()

    def _snapshot(self, session_id: str) -> dict:
        session = self._require_session(session_id)
        manager = session.manager
        libraries = []
        for library_id in manager.library_ids:
            label = LIBRARY_LABELS.get(library_id)
            if label is None and library_id.startswith("address.scope:"):
                label = "基站地址热词库"
            libraries.append({
                "id": library_id,
                "name": label or library_id,
            })
        return {
            "sessionId": session_id,
            "hotwordVersion": manager.hotword_version,
            "hotwordCount": manager.hotword_count,
            "handshakeTokenCount": len(manager.current_hotwords().split()),
            "hotwords": [
                {"text": text, "weight": weight}
                for text, weight in manager.current_hotword_items()
            ],
            "libraries": libraries,
            "warning": manager.warning_threshold_reached,
            "truncated": manager.truncated,
            "address": session.address_summary,
        }

    def _require_session(self, session_id: str) -> DemoSession:
        self._purge_sessions()
        session = self._sessions.get(str(session_id or "").strip())
        if session is None:
            raise KeyError("demo session not found")
        return session

    def _purge_sessions(self) -> None:
        cutoff = time.monotonic() - self._session_ttl_seconds
        self._sessions = {
            key: value
            for key, value in self._sessions.items()
            if value.created_at > cutoff
        }
        overflow = len(self._sessions) - self._max_sessions
        if overflow > 0:
            oldest = sorted(
                self._sessions.items(), key=lambda item: item[1].created_at
            )[:overflow]
            for session_id, _ in oldest:
                self._sessions.pop(session_id, None)


def wav_bytes_to_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(BytesIO(wav_bytes), "rb") as handle:
        if (
            handle.getframerate() != 16000
            or handle.getnchannels() != 1
            or handle.getsampwidth() != 2
        ):
            raise ValueError("WAV must be 16 kHz mono PCM s16le")
        return handle.readframes(handle.getnframes())


async def transcribe_funasr(
    session: aiohttp.ClientSession,
    *,
    ws_url: str,
    pcm: bytes,
    hotwords: str = "",
    timeout_seconds: float = 30,
) -> dict:
    """Transcribe one PCM stream and keep the longest streaming hypothesis."""
    started = time.perf_counter()
    texts: list[str] = []
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.ws_connect(
        ws_url,
        protocols=["binary"],
        timeout=timeout,
    ) as ws:
        handshake = {
            "chunk_size": [5, 10, 5],
            "wav_name": f"hotword_demo_{uuid.uuid4().hex[:8]}",
            "is_speaking": True,
            "chunk_interval": 10,
            "mode": "2pass",
            "itn": True,
            "audio_fs": 16000,
            "wav_format": "pcm",
        }
        if hotwords:
            handshake["hotwords"] = hotwords
        await ws.send_str(json.dumps(handshake, ensure_ascii=False))
        for offset in range(0, len(pcm), 3200):
            await ws.send_bytes(pcm[offset : offset + 3200])
        await ws.send_str(json.dumps({"is_speaking": False, "mode": "2pass"}))

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=1.5)
            except TimeoutError:
                break
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                text = str(payload.get("text") or "").strip()
                if text:
                    texts.append(text)
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            }:
                break
    return {
        "text": max(texts, key=len) if texts else "(无结果)",
        "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
        "hotwordCount": len(hotwords.split()) if hotwords else 0,
    }


__all__ = [
    "HotwordDemoService",
    "InvalidSceneSignal",
    "transcribe_funasr",
    "wav_bytes_to_pcm",
]
