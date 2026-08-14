"""Persist turn-level ASR speech records into PostgreSQL."""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

LOG = logging.getLogger("asr-database")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _fit_varchar(value: Any, max_length: int) -> str:
    text = _string(value)
    if len(text) <= max_length:
        return text
    without_hyphens = text.replace("-", "")
    if len(without_hyphens) <= max_length:
        return without_hyphens
    return without_hyphens[:max_length]


def _int_ms(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _ms_to_time_text(ms: int) -> str:
    total_seconds = max(0, int(ms // 1000)) % (24 * 60 * 60)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _timestamp_from_ms(ms: int) -> datetime:
    if ms <= 0:
        return datetime.now()
    return datetime.fromtimestamp(ms / 1000)


@dataclass(frozen=True)
class SpeechSegment:
    call_id: str
    segment_id: str
    text: str
    start_time_ms: int
    end_time_ms: int
    send_time_ms: int


@dataclass(frozen=True)
class AsrSpeechRecord:
    asr_id: str
    call_id: str
    segment_id: str
    call_from: str
    call_to: str
    speaker: str
    speech_content: str
    speech_url: str
    start_time: str
    end_time: str
    duration: int
    send_time: datetime
    create_by: str


class AsrDatabaseConfigMixin:
    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        schema: str = "ai",
        table: str = "asr_speech_recognition",
        connect_timeout: int = 5,
    ):
        self.enabled = enabled
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.table = table
        self.connect_timeout = connect_timeout

    def _qualified_table(self) -> str:
        if not _IDENTIFIER_RE.match(self.schema) or not _IDENTIFIER_RE.match(self.table):
            raise ValueError("invalid ASR DB schema/table identifier")
        return f"{self.schema}.{self.table}"

    def _connect(self):
        import psycopg2

        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=self.connect_timeout,
        )


class AsrDatabaseWriter:
    """Cache speech.final events and insert one DB row for each audio.segment turn."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        schema: str = "ai",
        table: str = "asr_speech_recognition",
        create_by: str = "asr-bridge",
        connect_timeout: int = 5,
        call_id_max_length: int = 36,
    ):
        self._config = AsrDatabaseConfigMixin(
            enabled=enabled,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            schema=schema,
            table=table,
            connect_timeout=connect_timeout,
        )
        self.enabled = self._config.enabled
        self.host = self._config.host
        self.port = self._config.port
        self.database = self._config.database
        self.user = self._config.user
        self.password = self._config.password
        self.schema = self._config.schema
        self.table = self._config.table
        self.create_by = create_by or "asr-bridge"
        self.connect_timeout = self._config.connect_timeout
        self.call_id_max_length = call_id_max_length
        self._segments: dict[tuple[str, str], SpeechSegment] = {}
        self._pending_audio: dict[tuple[str, str], dict[str, Any]] = {}
        self._persisted: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def remember_speech(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        segment = self._speech_segment(event)
        if not segment:
            return

        ready: list[tuple[tuple[str, str], dict[str, Any], AsrSpeechRecord]] = []
        with self._lock:
            self._segments[(segment.call_id, segment.segment_id)] = segment
            for key, audio_event in list(self._pending_audio.items()):
                record = self._record_from_audio_locked(audio_event)
                if record is not None:
                    ready.append((key, audio_event, record))
                    self._pending_audio.pop(key, None)

        for key, audio_event, record in ready:
            self._finish_insert(key, audio_event, record)

    def save_audio_turn(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        key = self._audio_key(event)
        if not key:
            return

        record: AsrSpeechRecord | None = None
        with self._lock:
            if key in self._persisted:
                return
            record = self._record_from_audio_locked(event)
            if record is None:
                self._pending_audio[key] = dict(event)
                return

        self._finish_insert(key, event, record)

    def _speech_segment(self, event: dict[str, Any]) -> SpeechSegment | None:
        call_id = _string(event.get("callId"))
        segment_id = _string(event.get("segmentId"))
        text = _string(event.get("text"))
        if not call_id or not segment_id or not text:
            return None
        return SpeechSegment(
            call_id=call_id,
            segment_id=segment_id,
            text=text,
            start_time_ms=_int_ms(event.get("startTimeMs")),
            end_time_ms=_int_ms(event.get("endTimeMs")),
            send_time_ms=_int_ms(event.get("sendTimeMs"), int(time.time() * 1000)),
        )

    def _audio_key(self, event: dict[str, Any]) -> tuple[str, str] | None:
        call_id = _string(event.get("callId"))
        segment_id = _string(event.get("segmentId"))
        if not call_id or not segment_id:
            return None
        return (call_id, segment_id)

    def _record_from_audio_locked(self, event: dict[str, Any]) -> AsrSpeechRecord | None:
        key = self._audio_key(event)
        if not key or key in self._persisted:
            return None
        call_id, segment_id = key
        speech_url = _string(event.get("audioUrl"))
        if not speech_url:
            return None

        segment_ids = [
            _string(item)
            for item in (event.get("segmentIds") or [segment_id])
            if _string(item)
        ]
        if not segment_ids:
            segment_ids = [segment_id]

        segments: list[SpeechSegment] = []
        for item_segment_id in segment_ids:
            segment = self._segments.get((call_id, item_segment_id))
            if segment is None:
                return None
            segments.append(segment)

        speech_content = " ".join(segment.text for segment in segments if segment.text).strip()
        if not speech_content:
            return None

        start_ms = _int_ms(event.get("startTimeMs"), segments[0].start_time_ms)
        end_ms = _int_ms(event.get("endTimeMs"), segments[-1].end_time_ms)
        duration_ms = _int_ms(event.get("audioDurationMs"), max(0, end_ms - start_ms))
        send_time_ms = _int_ms(event.get("sendTimeMs"), int(time.time() * 1000))
        return AsrSpeechRecord(
            asr_id=self._new_asr_id(send_time_ms),
            call_id=_fit_varchar(call_id, self.call_id_max_length),
            segment_id=_fit_varchar(segment_id, 32),
            call_from=_fit_varchar(event.get("callfrom"), 30),
            call_to=_fit_varchar(event.get("callto"), 30),
            speaker=_fit_varchar(event.get("speaker"), 50),
            speech_content=speech_content,
            speech_url=_fit_varchar(speech_url, 255),
            start_time=_ms_to_time_text(start_ms),
            end_time=_ms_to_time_text(end_ms),
            duration=max(0, int(math.ceil(duration_ms / 1000))) if duration_ms else 0,
            send_time=_timestamp_from_ms(send_time_ms),
            create_by=self.create_by,
        )

    def _finish_insert(self, key: tuple[str, str], audio_event: dict[str, Any], record: AsrSpeechRecord) -> None:
        if self._insert_record(record):
            with self._lock:
                self._persisted.add(key)
                self._pending_audio.pop(key, None)
            return
        with self._lock:
            if key not in self._persisted:
                self._pending_audio[key] = dict(audio_event)

    def _new_asr_id(self, send_time_ms: int) -> str:
        base = str(send_time_ms or int(time.time() * 1000))
        return f"{base}{uuid.uuid4().hex[:6]}"[:32]

    def _qualified_table(self) -> str:
        return self._config._qualified_table()

    def _insert_record(self, record: AsrSpeechRecord) -> bool:
        try:
            import psycopg2
        except ImportError:
            LOG.error("ASR_DB_ENABLED=true but psycopg2 is not installed")
            return False

        sql = f"""
            INSERT INTO {self._qualified_table()} (
                asr_id, call_id, segment_id, call_from, call_to, speaker,
                speech_content, speech_url, start_time, end_time, duration,
                send_time, create_by
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s
            )
        """
        params = (
            record.asr_id,
            record.call_id,
            record.segment_id,
            record.call_from,
            record.call_to,
            record.speaker,
            record.speech_content,
            record.speech_url,
            record.start_time,
            record.end_time,
            record.duration,
            record.send_time,
            record.create_by,
        )
        try:
            with self._config._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
            LOG.info("ASR speech persisted callId=%s segmentId=%s", record.call_id, record.segment_id)
            return True
        except Exception:
            LOG.exception("ASR speech persist failed callId=%s segmentId=%s", record.call_id, record.segment_id)
            return False


class AsrDatabaseReader:
    """Read persisted ASR turn records for transfer/history replay."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        schema: str = "ai",
        table: str = "asr_speech_recognition",
        connect_timeout: int = 5,
        default_limit: int = 500,
        max_limit: int = 1000,
    ):
        self._config = AsrDatabaseConfigMixin(
            enabled=enabled,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            schema=schema,
            table=table,
            connect_timeout=connect_timeout,
        )
        self.enabled = self._config.enabled
        self.host = self._config.host
        self.port = self._config.port
        self.database = self._config.database
        self.user = self._config.user
        self.password = self._config.password
        self.schema = self._config.schema
        self.table = self._config.table
        self.connect_timeout = self._config.connect_timeout
        self.default_limit = default_limit
        self.max_limit = max_limit

    def list_records(self, call_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("ASR database is disabled")
        call_id = _string(call_id)
        if not call_id:
            return []
        row_limit = self._normalize_limit(limit)
        sql = f"""
            SELECT
                asr_id, call_id, segment_id, call_from, call_to, speaker,
                speech_content, speech_url, start_time, end_time, duration,
                send_time, create_by
            FROM {self._config._qualified_table()}
            WHERE call_id = %s
            ORDER BY send_time ASC, start_time ASC, segment_id ASC
            LIMIT %s
        """
        try:
            with self._config._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (call_id, row_limit))
                    rows = cur.fetchall()
        except ImportError:
            LOG.error("ASR_DB_ENABLED=true but psycopg2 is not installed")
            raise
        except Exception:
            LOG.exception("ASR speech query failed callId=%s", call_id)
            raise
        return [self._row_to_dict(row) for row in rows]

    def _normalize_limit(self, value: int | None) -> int:
        if value is None:
            return self.default_limit
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return self.default_limit
        return max(1, min(limit, self.max_limit))

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        columns = [
            "asr_id", "call_id", "segment_id", "call_from", "call_to", "speaker",
            "speech_content", "speech_url", "start_time", "end_time", "duration",
            "send_time", "create_by",
        ]
        if isinstance(row, dict):
            values = row
        else:
            values = dict(zip(columns, row))
        send_time = values.get("send_time")
        if hasattr(send_time, "isoformat"):
            send_time_value = send_time.isoformat(sep=" ")
        else:
            send_time_value = _string(send_time)
        return {
            "asrId": _string(values.get("asr_id")),
            "callId": _string(values.get("call_id")),
            "segmentId": _string(values.get("segment_id")),
            "callfrom": _string(values.get("call_from")),
            "callto": _string(values.get("call_to")),
            "speaker": _string(values.get("speaker")),
            "text": _string(values.get("speech_content")),
            "audioUrl": _string(values.get("speech_url")),
            "startTime": _string(values.get("start_time")),
            "endTime": _string(values.get("end_time")),
            "duration": int(values.get("duration") or 0),
            "sendTime": send_time_value,
            "createBy": _string(values.get("create_by")),
        }


def create_asr_database_writer() -> AsrDatabaseWriter:
    return AsrDatabaseWriter(
        enabled=_env_bool("ASR_DB_ENABLED"),
        host=os.getenv("ASR_DB_HOST", "").strip(),
        port=int(os.getenv("ASR_DB_PORT", "5432")),
        database=os.getenv("ASR_DB_NAME", "").strip(),
        user=os.getenv("ASR_DB_USER", "").strip(),
        password=os.getenv("ASR_DB_PASS", ""),
        schema=os.getenv("ASR_DB_SCHEMA", "ai").strip() or "ai",
        table=os.getenv("ASR_DB_TABLE", "asr_speech_recognition").strip() or "asr_speech_recognition",
        create_by=os.getenv("ASR_DB_CREATE_BY", "asr-bridge").strip() or "asr-bridge",
        connect_timeout=int(os.getenv("ASR_DB_CONNECT_TIMEOUT", "5")),
        call_id_max_length=int(os.getenv("ASR_DB_CALL_ID_MAX_LENGTH", "36")),
    )


def create_asr_database_reader() -> AsrDatabaseReader:
    return AsrDatabaseReader(
        enabled=_env_bool("ASR_DB_ENABLED"),
        host=os.getenv("ASR_DB_HOST", "").strip(),
        port=int(os.getenv("ASR_DB_PORT", "5432")),
        database=os.getenv("ASR_DB_NAME", "").strip(),
        user=os.getenv("ASR_DB_USER", "").strip(),
        password=os.getenv("ASR_DB_PASS", ""),
        schema=os.getenv("ASR_DB_SCHEMA", "ai").strip() or "ai",
        table=os.getenv("ASR_DB_TABLE", "asr_speech_recognition").strip() or "asr_speech_recognition",
        connect_timeout=int(os.getenv("ASR_DB_CONNECT_TIMEOUT", "5")),
        default_limit=int(os.getenv("ASR_DB_QUERY_DEFAULT_LIMIT", "500")),
        max_limit=int(os.getenv("ASR_DB_QUERY_MAX_LIMIT", "1000")),
    )
