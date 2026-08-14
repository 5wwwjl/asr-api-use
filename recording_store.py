"""Persistent WAV storage for VAD-delimited ASR speaker segments."""

from __future__ import annotations

import os
import re
import threading
import json
import base64
import uuid
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import math
from pathlib import Path
import sys
from urllib import request
from urllib.parse import quote
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RECORDINGS_DIR = Path(
    os.getenv("ASR_RECORDINGS_DIR", str(BASE_DIR / "recordings"))
).resolve()
_SAFE_COMPONENT_RE = re.compile(r"[^0-9A-Za-z._-]+")


@dataclass(frozen=True)
class SavedRecording:
    path: Path | None
    audio_url: str
    duration_ms: int
    start_time_ms: int
    end_time_ms: int
    record_id: str | None = None


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("_", str(value or "")).strip("._-")
    return cleaned or fallback


def _segment_timing(pcm: bytes, sample_rate: int, sample_width: int, channels: int, start_time_ms: int, end_time_ms: int) -> tuple[int, int, int]:
    duration_ms = round(len(pcm) * 1000 / (sample_rate * sample_width * channels))
    resolved_end_ms = max(int(end_time_ms or 0), int(start_time_ms or 0) + duration_ms)
    resolved_start_ms = max(0, resolved_end_ms - duration_ms)
    return duration_ms, resolved_start_ms, resolved_end_ms


def _wav_bytes(pcm: bytes, *, sample_rate: int, sample_width: int, channels: int) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class RecordingStore:
    """Write permanent mono PCM segments as browser-playable WAV files."""

    def __init__(
        self,
        root: Path | str = DEFAULT_RECORDINGS_DIR,
        *,
        url_prefix: str = "/recordings",
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        normalize_below_db: float = -30.0,
        normalize_above_db: float = -50.0,
        normalize_target_db: float = -22.0,
        normalize_peak_db: float = -1.0,
        normalize_max_gain_db: float = 18.0,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.url_prefix = "/" + url_prefix.strip("/")
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.normalize_below_db = normalize_below_db
        self.normalize_above_db = normalize_above_db
        self.normalize_target_db = normalize_target_db
        self.normalize_peak_db = normalize_peak_db
        self.normalize_max_gain_db = normalize_max_gain_db
        self._sequences: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()

    def save_segment(
        self,
        pcm: bytes,
        *,
        call_id: str,
        speaker: str,
        start_time_ms: int = 0,
        end_time_ms: int = 0,
        recorded_at: datetime | None = None,
    ) -> SavedRecording:
        if not pcm:
            raise ValueError("cannot save an empty audio segment")

        recorded_at = recorded_at or datetime.now()
        day = recorded_at.strftime("%Y-%m-%d")
        safe_call_id = _safe_component(call_id, "unknown-call")
        safe_speaker = _safe_component(speaker, "unknown")
        duration_ms, resolved_start_ms, resolved_end_ms = _segment_timing(
            pcm, self.sample_rate, self.sample_width, self.channels, start_time_ms, end_time_ms
        )
        directory = (self.root / day / safe_call_id).resolve()
        if self.root != directory and self.root not in directory.parents:
            raise ValueError("recording path escaped storage root")
        directory.mkdir(parents=True, exist_ok=True)

        key = (day, safe_call_id, safe_speaker)
        with self._lock:
            sequence = self._sequences.get(key, 0) + 1
            while True:
                filename = (
                    f"{safe_speaker}-{sequence:04d}-"
                    f"{resolved_start_ms:010d}-{resolved_end_ms:010d}.wav"
                )
                path = directory / filename
                if not path.exists():
                    self._sequences[key] = sequence
                    break
                sequence += 1

        wav_pcm = self._normalize_pcm(pcm)
        path.write_bytes(_wav_bytes(
            wav_pcm,
            sample_rate=self.sample_rate,
            sample_width=self.sample_width,
            channels=self.channels,
        ))

        relative = path.relative_to(self.root).as_posix()
        return SavedRecording(
            path=path,
            audio_url=f"{self.url_prefix}/{relative}",
            duration_ms=duration_ms,
            start_time_ms=resolved_start_ms,
            end_time_ms=resolved_end_ms,
        )

    def _normalize_pcm(self, pcm: bytes) -> bytes:
        if self.sample_width != 2 or len(pcm) < 2:
            return pcm

        samples = array("h")
        samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return pcm

        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        peak = max(abs(sample) for sample in samples)
        if rms <= 0 or peak <= 0:
            return pcm

        rms_db = 20 * math.log10(rms / 32768.0)
        if rms_db < self.normalize_above_db or rms_db >= self.normalize_below_db:
            return pcm

        target_gain = 10 ** ((self.normalize_target_db - rms_db) / 20)
        peak_limit = 32767 * (10 ** (self.normalize_peak_db / 20))
        peak_gain = peak_limit / peak
        max_gain = 10 ** (self.normalize_max_gain_db / 20)
        gain = min(target_gain, peak_gain, max_gain)
        if gain <= 1.0:
            return pcm

        for idx, sample in enumerate(samples):
            samples[idx] = max(-32768, min(32767, round(sample * gain)))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()


class OpenApiRecordingStore(RecordingStore):
    """Upload WAV segments to an OpenAPI file service instead of persisting locally."""

    def __init__(
        self,
        upload_url: str,
        *,
        upload_field: str = "file",
        upload_mode: str = "multipart",
        timeout: float = 10.0,
        download_url_template: str = "",
        extra_headers: Mapping[str, str] | None = None,
        disable_proxy: bool = False,
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        normalize_below_db: float = -30.0,
        normalize_above_db: float = -50.0,
        normalize_target_db: float = -22.0,
        normalize_peak_db: float = -1.0,
        normalize_max_gain_db: float = 18.0,
    ):
        self.upload_url = upload_url
        self.upload_field = upload_field
        self.upload_mode = upload_mode
        self.timeout = timeout
        self.download_url_template = download_url_template
        self.extra_headers = dict(extra_headers or {})
        self.disable_proxy = disable_proxy
        self._opener = request.build_opener(request.ProxyHandler({})) if disable_proxy else None
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.normalize_below_db = normalize_below_db
        self.normalize_above_db = normalize_above_db
        self.normalize_target_db = normalize_target_db
        self.normalize_peak_db = normalize_peak_db
        self.normalize_max_gain_db = normalize_max_gain_db

    def save_segment(
        self,
        pcm: bytes,
        *,
        call_id: str,
        speaker: str,
        start_time_ms: int = 0,
        end_time_ms: int = 0,
        recorded_at: datetime | None = None,
    ) -> SavedRecording:
        if not pcm:
            raise ValueError("cannot upload an empty audio segment")

        duration_ms, resolved_start_ms, resolved_end_ms = _segment_timing(
            pcm, self.sample_rate, self.sample_width, self.channels, start_time_ms, end_time_ms
        )
        safe_speaker = _safe_component(speaker, "unknown")
        filename = f"{safe_speaker}-{resolved_start_ms:010d}-{resolved_end_ms:010d}.wav"
        wav_pcm = self._normalize_pcm(pcm)
        wav_data = _wav_bytes(
            wav_pcm,
            sample_rate=self.sample_rate,
            sample_width=self.sample_width,
            channels=self.channels,
        )
        payload = self._upload(wav_data, filename)
        record_id = self._extract_record_id(payload)
        audio_url = self._audio_url(record_id)
        return SavedRecording(
            path=None,
            audio_url=audio_url,
            duration_ms=duration_ms,
            start_time_ms=resolved_start_ms,
            end_time_ms=resolved_end_ms,
            record_id=record_id,
        )

    def _request_headers(self, content_type: str) -> dict[str, str]:
        headers = {"Content-Type": content_type}
        headers.update(self.extra_headers)
        return headers

    def _upload(self, wav_data: bytes, filename: str) -> dict:
        if self.upload_mode == "json_base64":
            body = json.dumps({self.upload_field: base64.b64encode(wav_data).decode("ascii")}).encode("utf-8")
            req = request.Request(
                self.upload_url,
                data=body,
                headers=self._request_headers("application/json"),
                method="POST",
            )
        else:
            boundary = f"----asr-recording-{uuid.uuid4().hex}"
            body = b"\r\n".join([
                f"--{boundary}".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{self.upload_field}"; '
                    f'filename="{filename}"'
                ).encode("utf-8"),
                b"Content-Type: audio/wav",
                b"",
                wav_data,
                f"--{boundary}--".encode("ascii"),
                b"",
            ])
            req = request.Request(
                self.upload_url,
                data=body,
                headers=self._request_headers(f"multipart/form-data; boundary={boundary}"),
                method="POST",
            )

        open_request = self._opener.open if self._opener else request.urlopen
        with open_request(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"record upload returned non-json response: {raw[:200]}") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"record upload failed: {payload.get('message') or payload}")
        return payload

    def _extract_record_id(self, payload: dict) -> str:
        candidates = [
            payload.get("data") if isinstance(payload, dict) else None,
            payload.get("id") if isinstance(payload, dict) else None,
            payload.get("recordId") if isinstance(payload, dict) else None,
        ]
        data = candidates[0]
        if isinstance(data, dict):
            candidates.extend([data.get("id"), data.get("recordId"), data.get("fileId")])
        for value in candidates:
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        raise RuntimeError(f"record upload response did not contain a record id: {payload}")

    def _audio_url(self, record_id: str) -> str:
        if not self.download_url_template:
            return ""
        return self.download_url_template.format(record_id=quote(record_id, safe=""), id=quote(record_id, safe=""))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _openapi_extra_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    host_header = os.getenv("ASR_RECORDING_UPLOAD_HOST_HEADER", "").strip()
    if host_header:
        headers["Host"] = host_header

    tenant_id = os.getenv("ASR_RECORDING_TENANT_ID", "").strip()
    if tenant_id:
        headers["tenantId"] = tenant_id

    token = (
        os.getenv("ASR_RECORDING_AUTH_TOKEN", "").strip()
        or os.getenv("ASR_RECORDING_UPLOAD_AUTH_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    return headers


def create_recording_store():
    store_type = os.getenv("ASR_RECORDING_STORE", "local").strip().lower()
    upload_url = os.getenv("ASR_RECORDING_UPLOAD_URL", "").strip()
    if store_type == "openapi" or upload_url:
        if not upload_url:
            raise RuntimeError("ASR_RECORDING_UPLOAD_URL is required when ASR_RECORDING_STORE=openapi")
        return OpenApiRecordingStore(
            upload_url,
            upload_mode=os.getenv("ASR_RECORDING_UPLOAD_MODE", "multipart").strip() or "multipart",
            upload_field=os.getenv("ASR_RECORDING_UPLOAD_FIELD", "file").strip() or "file",
            timeout=float(os.getenv("ASR_RECORDING_UPLOAD_TIMEOUT", "10")),
            download_url_template=os.getenv("ASR_RECORDING_DOWNLOAD_URL_TEMPLATE", "").strip(),
            extra_headers=_openapi_extra_headers(),
            disable_proxy=_env_bool("ASR_RECORDING_UPLOAD_NO_PROXY"),
        )
    return RecordingStore()
