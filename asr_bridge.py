"""
ASR 协议翻译模块 — 外部统一消息格式 ↔ FunASR 原始 PCM 协议。

导入到 https_gateway.py 使用，不单独启动进程。
"""

import asyncio
import base64
import json
import logging
import math
import os
import re
import time

from aiohttp import WSMsgType
from aiohttp.client_exceptions import ClientConnectionResetError

from call_state import CallHoldState
from asr_database import create_asr_database_writer
from asr_message_service import create_asr_message_service_publisher
from audio_preprocessor import AudioPreprocessor
from asr_ai_postprocessor import TranscriptTurn, create_asr_call_postprocessor
from asr_address_scope import (
    AddressScopeBinding,
    AddressScopeBindingStore,
    AddressScopeDispatch,
)
from asr_address_scope_client import AddressScopeClient, AddressScopeQueryError
from asr_address_scope_audit import AddressScopeAuditStore
from hotword_manager import HotwordManager, InvalidSceneSignal
from asr_rabbitmq import create_asr_rabbitmq_publisher
from asr_providers import (
    ASRProviderFactory,
    FUNASR,
    XFYUN,
    ProviderError,
    ProviderResult,
    VALID_PROVIDERS,
)
from recording_store import RecordingStore, create_recording_store
from turn_recording_coordinator import (
    CompletedTurnRecording,
    RecordingChunk,
    TurnRecordingCoordinator,
    pair_key,
)
from vad_engine import VADEngine, VADConfig

LOG = logging.getLogger("asr_bridge")

BASE_DIR = os.path.dirname(__file__)
ASR_BUSINESS_LOG_DIR = os.environ.get("ASR_BUSINESS_LOG_DIR", os.path.join(BASE_DIR, "logs"))
_BUSINESS_LOGGERS: dict[str, logging.Logger] = {}


def _read_env_file(env_path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(env_path):
        return values
    with open(env_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def _load_local_env() -> None:
    for key, value in _read_env_file(os.path.join(BASE_DIR, ".env")).items():
        if key not in os.environ:
            os.environ[key] = value

    # Location keeps downstream credentials in its own mode-600 env file. Map
    # only the hotword-reader credentials so ASR does not duplicate secrets.
    credential_file = os.getenv(
        "ASR_ADDRESS_SCOPE_CREDENTIAL_ENV_FILE", ""
    ).strip()
    if not credential_file:
        return
    credentials = _read_env_file(credential_file)
    credential_mapping = {
        "ASR_ADDRESS_SCOPE_MQ_USER": "LOCATION_RABBITMQ_HOTWORD_USER",
        "ASR_ADDRESS_SCOPE_MQ_PASSWORD": "LOCATION_RABBITMQ_HOTWORD_PASSWORD",
        "ASR_ADDRESS_SCOPE_DB_USER": "LOCATION_DB_HOTWORD_USER",
        "ASR_ADDRESS_SCOPE_DB_PASSWORD": "LOCATION_DB_HOTWORD_PASSWORD",
    }
    for target, source in credential_mapping.items():
        value = credentials.get(source, "")
        if target not in os.environ and value:
            os.environ[target] = value


_load_local_env()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        LOG.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, value, default)
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        LOG.warning("环境变量 %s=%r 不是数字，使用默认值 %s", name, value, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


# FunASR 返回文本中附带的情绪/语种 token，需要过滤
_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_BUSINESS_ITN_REPLACEMENTS = (
    (re.compile(r"[幺么一][幺么一]九"), "119"),
)


def _normalize_asr_text(raw: str) -> str:
    text = _TOKEN_RE.sub("", str(raw or "")).strip()
    # Paraformer 有时会在每个中文/数字字符之间插入空格。
    # 清掉中日韩字符/数字之间的空格，但保留英文单词之间的正常空格。
    text = re.sub(r"(?<=[\u4e00-\u9fff0-9])\s+(?=[\u4e00-\u9fff0-9])", "", text)
    for pattern, replacement in _BUSINESS_ITN_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text.strip()


# ── 热词加载 ────────────────────────────────────────────
def _load_hotwords(filepath: str | None = None) -> str:
    """从 hotwords.txt 加载热词，返回空格分隔的字符串用于握手。"""
    if filepath is None:
        filepath = os.environ.get(
            "ASR_HOTWORD_FILE",
            os.path.join(BASE_DIR, "hotwords.txt"),
        )
    if not os.path.exists(filepath):
        LOG.warning("热词文件不存在: %s，握手不传 hotwords", filepath)
        return ""

    words = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.rsplit(" ", 1)
            word = parts[0].strip()
            if word:
                words.append(word)

    if words:
        LOG.info("已加载 %d 个热词 (来自 %s)", len(words), filepath)
    return " ".join(words)


_HOTWORD_STRING: str = _load_hotwords()


def normalize_asr_project(value: str | None = None, *, call_id: str | None = None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    call = str(call_id or "").strip().lower().replace("-", "_")
    if raw in {"addressbot", "address_bot", "address"} or "addressbot" in raw or "address_bot" in raw:
        return "addressbot"
    if "addressbot" in call or "address_bot" in call:
        return "addressbot"
    return "firebot"


def _business_logger(project: str) -> logging.Logger:
    project = normalize_asr_project(project)
    logger = _BUSINESS_LOGGERS.get(project)
    if logger is not None:
        return logger

    os.makedirs(ASR_BUSINESS_LOG_DIR, exist_ok=True)
    logger = logging.getLogger(f"asr_business.{project}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(
            os.path.join(ASR_BUSINESS_LOG_DIR, f"{project}.log"),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    _BUSINESS_LOGGERS[project] = logger
    return logger


def log_business_asr_event(event: dict) -> None:
    if not isinstance(event, dict):
        return
    event_name = str(event.get("event") or event.get("eventType") or "").strip()
    if not event_name:
        return

    call_id = str(event.get("callId") or event.get("call_id") or "").strip()
    project = normalize_asr_project(event.get("project"), call_id=call_id)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    line = {
        "project": project,
        "event": event_name,
        "callId": call_id,
        "segmentId": event.get("segmentId") or payload.get("segmentId") or "",
        "callfrom": event.get("callfrom") or "",
        "callto": event.get("callto") or "",
        "speaker": event.get("speaker") or payload.get("speaker") or "",
        "direction": event.get("direction") or payload.get("direction") or "",
        "text": event.get("text") or payload.get("text") or "",
        "correctedText": event.get("correctedText") or "",
        "finalSource": event.get("finalSource") or "",
        "sendTimeMs": event.get("sendTimeMs") or int(time.time() * 1000),
    }
    if event_name == "speech.final":
        providers = event.get("providers") if isinstance(event.get("providers"), list) else []
        line.update({
            "provider": event.get("provider") or payload.get("provider") or "",
            "providers": providers,
        })
    if event_name == "call.corrected":
        turns = event.get("turns") if isinstance(event.get("turns"), list) else []
        line.update({
            "turns": turns,
            "keywords": event.get("keywords") if isinstance(event.get("keywords"), list) else [],
            "correctionProvider": event.get("correctionProvider") or "",
            "correctionMode": event.get("correctionMode") or "",
            "highlightProvider": event.get("highlightProvider") or "",
            "replacements": event.get("replacements") if isinstance(event.get("replacements"), list) else [],
            "dbElapsedMs": event.get("dbElapsedMs"),
            "highlightElapsedMs": event.get("highlightElapsedMs"),
            "highlightFailed": bool(event.get("highlightFailed")),
            "llmElapsedMs": event.get("llmElapsedMs"),
            "llmHighlightElapsedMs": event.get("llmHighlightElapsedMs"),
            "llmHighlightFailed": bool(event.get("llmHighlightFailed")),
            "ruleHighlightElapsedMs": event.get("ruleHighlightElapsedMs"),
            "correctionScope": event.get("correctionScope") or "",
            "segmentIds": event.get("segmentIds") if isinstance(event.get("segmentIds"), list) else [],
        })
    try:
        _business_logger(project).info(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        LOG.exception("业务 ASR 日志写入失败, project=%s callId=%s event=%s", project, call_id, event_name)


# ── Monitor WebSocket 注册表 ───────────────────────────────

_monitor_sockets: set = set()
_monitor_lock = asyncio.Lock()
_RABBITMQ_EVENT_ALLOWLIST = {"speech.final", "audio.segment", "call.corrected", "call.history"}
# 旧 ids:asr 发布器仅保留为显式启用的应急回退，不参与正常事件扇出。
_rabbitmq_publisher = create_asr_rabbitmq_publisher()
# 问询部门 ids:qs 通道保持原协议和路由不变。
_qs_rabbitmq_publisher = create_asr_rabbitmq_publisher(
    env_prefix="ASR_RABBITMQ_QS_",
    default_exchange="ids:qs",
    default_source="ids:qs",
    default_routing_prefix="qs",
)
_RABBITMQ_PUBLISHERS = [p for p in (_qs_rabbitmq_publisher,) if p is not None]
_message_service_publisher = create_asr_message_service_publisher(
    fallback_publisher=_rabbitmq_publisher
)


async def start_message_service_publisher() -> bool:
    return await _message_service_publisher.start()


async def close_message_service_publisher() -> None:
    await _message_service_publisher.close()


_database_writer = create_asr_database_writer()
_call_postprocessor = create_asr_call_postprocessor()
_call_hold_state = CallHoldState()
_ai_correction_semaphore = asyncio.Semaphore(_env_int("ASR_AI_CORRECTION_CONCURRENCY", 1))
_turn_recording_coordinator = TurnRecordingCoordinator()
_global_speech_events: dict[tuple[str, str, str], dict] = {}
_global_pending_completed_texts: dict[tuple[str, str, tuple[str, ...]], CompletedTurnRecording] = {}
_model_preferences: dict[str, tuple[str, float]] = {}
_MODEL_PREFERENCE_TTL_SECONDS = max(
    60.0,
    _env_float("ASR_MODEL_PREFERENCE_TTL_SECONDS", 3600.0),
)


def _prune_model_preferences(now: float | None = None) -> None:
    cutoff = (time.monotonic() if now is None else now) - _MODEL_PREFERENCE_TTL_SECONDS
    for call_id, (_, remembered_at) in list(_model_preferences.items()):
        if remembered_at < cutoff:
            _model_preferences.pop(call_id, None)


def _remember_model_preference(call_id: str, provider: str) -> None:
    call_id = str(call_id or "").strip()
    provider = str(provider or "").strip().lower()
    if not call_id or provider not in VALID_PROVIDERS:
        return
    now = time.monotonic()
    _prune_model_preferences(now)
    _model_preferences[call_id] = (provider, now)


def _model_preference(call_id: str) -> str | None:
    call_id = str(call_id or "").strip()
    if not call_id:
        return None
    now = time.monotonic()
    _prune_model_preferences(now)
    remembered = _model_preferences.get(call_id)
    return remembered[0] if remembered else None


def _forget_model_preference(call_id: str) -> None:
    _model_preferences.pop(str(call_id or "").strip(), None)


def _purge_global_call_state(call_id: str) -> None:
    call_id = str(call_id or "").strip()
    if not call_id:
        return
    for key, event in list(_global_speech_events.items()):
        key_call_id = key[1] if isinstance(key, tuple) and len(key) >= 3 else ""
        if key_call_id == call_id or str(event.get("callId") or "") == call_id:
            _global_speech_events.pop(key, None)
    for key, completed in list(_global_pending_completed_texts.items()):
        key_call_id = key[1] if isinstance(key, tuple) and len(key) >= 2 else ""
        if key_call_id == call_id or completed.call_id == call_id:
            _global_pending_completed_texts.pop(key, None)


async def register_monitor(ws) -> None:
    """注册一个浏览器监控 WebSocket，接收 Bridge 事件广播。"""
    async with _monitor_lock:
        _monitor_sockets.add(ws)


async def unregister_monitor(ws) -> None:
    """从广播集合中移除监控 WebSocket。"""
    async with _monitor_lock:
        _monitor_sockets.discard(ws)


def _monitor_sockets_snapshot() -> list:
    """返回当前监控 socket 的快照副本（线程安全只读迭代）。"""
    return list(_monitor_sockets)


def monitor_socket_count() -> int:
    return len([ws for ws in _monitor_sockets_snapshot() if not getattr(ws, "closed", False)])


async def _send_monitor_payloads(payloads: list[dict]) -> None:
    sockets = _monitor_sockets_snapshot()
    if not sockets:
        return
    dead = []
    encoded_payloads = [json.dumps(payload, ensure_ascii=False) for payload in payloads]
    for ws in sockets:
        try:
            if not ws.closed:
                for payload in encoded_payloads:
                    await ws.send_str(payload)
            else:
                dead.append(ws)
        except Exception:
            dead.append(ws)
    if dead:
        async with _monitor_lock:
            for ws in dead:
                _monitor_sockets.discard(ws)


async def _broadcast_to_monitors(event: dict, *, publish_to_rabbitmq: bool = True) -> None:
    """广播 ASR 事件，并将稳定事件推送消息服务和 ids:qs。"""
    log_business_asr_event(event)
    rabbitmq_messages = []
    should_publish = publish_to_rabbitmq and event.get("event") in _RABBITMQ_EVENT_ALLOWLIST
    if should_publish:
        if _message_service_publisher.config.enabled:
            callto = str(event.get("callto") or "").strip()
            if not callto:
                LOG.warning(
                    "消息服务跳过空 callto, event=%s callId=%s",
                    event.get("event"),
                    event.get("callId", ""),
                )
            elif not _message_service_publisher.enqueue(event):
                LOG.warning(
                    "消息服务事件未入队, event=%s callId=%s callto=%s",
                    event.get("event"),
                    event.get("callId", ""),
                    callto,
                )
        for publisher in _RABBITMQ_PUBLISHERS:
            try:
                msg = publisher.publish(event)
                if msg:
                    rabbitmq_messages.append(msg)
            except Exception:
                LOG.exception("RabbitMQ 广播异常, exchange=%s event=%s",
                              publisher.exchange, event.get("event"))

    payloads = []
    for rmq_msg in rabbitmq_messages:
        payloads.append({
            "event": "rabbitmq.message",
            "exchange": rmq_msg.get("exchange", ""),
            "routingKey": rmq_msg.get("routingKey", ""),
            "payload": rmq_msg.get("payload", {}),
        })
    payloads.append(event)
    await _send_monitor_payloads(payloads)


async def broadcast_call_history(
    call_id: str,
    records: list[dict],
    *,
    callfrom: str = "",
    callto: str = "",
) -> dict:
    """Broadcast persisted call history to monitor clients for transfer replay."""
    monitor_count = monitor_socket_count()
    if not callfrom and records:
        callfrom = str(records[0].get("callfrom") or "").strip()
    if not callto and records:
        callto = str(records[0].get("callto") or "").strip()
    event = {
        "event": "call.history",
        "callId": str(call_id or "").strip(),
        "callfrom": str(callfrom or "").strip(),
        "callto": str(callto or "").strip(),
        "records": records,
        "count": len(records),
        "monitorCount": monitor_count,
        "sendTimeMs": int(time.time() * 1000),
    }
    await _broadcast_to_monitors(event, publish_to_rabbitmq=True)
    return event


async def apply_cti_hold_event(event: dict) -> dict:
    """Apply a CTI hold/cancel event and broadcast the state change."""
    change = _call_hold_state.apply_cti_event(event)
    if change is None:
        return {"accepted": True, "changed": False}

    monitor_event = {
        "event": "call.hold.started" if change.status == "holding" else "call.hold.ended",
        "eventId": change.event_id,
        "callId": change.call_id,
        "callfrom": change.ext.get("from") or change.ext.get("callerId", ""),
        "callto": change.ext.get("to") or change.ext.get("extId", ""),
        "ext": change.ext,
        "hold": change.status == "holding",
        "status": change.status,
        "eventTime": change.event_time,
        "sendTimeMs": int(time.time() * 1000),
        "message": "通话保持中，转写暂停" if change.status == "holding" else "通话已恢复，转写继续",
    }
    await _broadcast_to_monitors(monitor_event, publish_to_rabbitmq=False)
    return {"accepted": True, "changed": True, "state": monitor_event}


# ── 活跃会话管理（支持前端强制结束）───────────────────────────

_active_sessions: dict[str, "BridgeSession"] = {}
_session_lock = asyncio.Lock()
_address_scope_store = AddressScopeBindingStore(
    ttl_seconds=max(1.0, _env_float("ASR_ADDRESS_SCOPE_PENDING_TTL_SECONDS", 1800.0)),
    max_entries=max(1, _env_int("ASR_ADDRESS_SCOPE_PENDING_MAX_ENTRIES", 10000)),
)
_address_scope_client = AddressScopeClient()
_address_scope_audit = AddressScopeAuditStore()


async def _apply_address_scope_hotwords(
    session: "BridgeSession", binding: AddressScopeBinding
) -> None:
    try:
        result = await _address_scope_client.fetch_hotwords(binding)
        _address_scope_audit.record_resolved(event_id=binding.event_id, result=result)
        changed = session.set_address_scope_hotwords(binding, result.hotwords)
        _address_scope_audit.record_applied(event_id=binding.event_id, changed=changed)
    except Exception as exc:
        _address_scope_audit.record_failed(
            event_id=binding.event_id, stage="address_scope_query_or_apply", error=exc
        )
        raise
    LOG.info(
        "address hotwords queued callId=%s scopeId=%s eventId=%s items=%d words=%d "
        "changed=%s queryMs=%.3f effective_from=next_segment",
        binding.call_id,
        binding.scope_id,
        binding.event_id,
        result.item_count,
        len(result.hotwords),
        changed,
        result.query_ms,
    )


async def accept_address_scope_event(payload: dict[str, object]) -> AddressScopeDispatch:
    """Associate a validated location scope with its active or future ASR call."""
    async with _session_lock:
        call_id = ""
        data = payload.get("data")
        if isinstance(data, dict):
            call_id = str(data.get("sessionId") or "").strip()
        session = _active_sessions.get(call_id)
        dispatch = _address_scope_store.accept(
            payload, call_active=session is not None and not session.call_ended
        )
        _address_scope_audit.record_received(
            event_id=dispatch.binding.event_id, payload=payload, dispatch=dispatch
        )
        if dispatch.status == "bound_active" and session is not None:
            session.set_address_scope_binding(dispatch.binding)

    if dispatch.status == "bound_active" and session is not None:
        try:
            await _apply_address_scope_hotwords(session, dispatch.binding)
        except AddressScopeQueryError:
            _address_scope_store.forget_event(dispatch.binding.event_id)
            raise

    LOG.info(
        "address scope event status=%s eventId=%s callId=%s scopeId=%s inventoryVersion=%s",
        dispatch.status,
        dispatch.binding.event_id,
        dispatch.binding.call_id,
        dispatch.binding.scope_id,
        dispatch.binding.inventory_version,
    )
    return dispatch


async def register_session(session: "BridgeSession") -> None:
    previous = None
    async with _session_lock:
        previous = _active_sessions.get(session.call_id)
        _active_sessions[session.call_id] = session
        pending = _address_scope_store.take_pending(session.call_id)
        if pending is not None:
            session.set_address_scope_binding(pending)
            LOG.info(
                "address scope pending binding attached callId=%s scopeId=%s eventId=%s",
                session.call_id,
                pending.scope_id,
                pending.event_id,
            )
            session._track_background_task(
                asyncio.create_task(_apply_address_scope_hotwords(session, pending)),
                "address-scope-hotwords",
            )

    if previous is not None and previous is not session:
        LOG.warning(
            "duplicate active callId replaced; closing stale session callId=%s",
            session.call_id,
        )
        await previous.force_end(preserve_call_state=True)


async def unregister_session(call_id: str, session=None) -> bool:
    async with _session_lock:
        current = _active_sessions.get(call_id)
        if session is None or current is session:
            _active_sessions.pop(call_id, None)
            return current is not None
        return False


async def active_session_model_states() -> list[dict]:
    """Return a stable snapshot used when a monitor connects or reconnects."""
    async with _session_lock:
        sessions = list(_active_sessions.values())
    return [session.model_state() for session in sessions if not session.call_ended]


def _paired_switch_rejection(
    request_id: str,
    target_provider: str,
    message: str,
    *,
    missing_call_ids: list[str] | None = None,
) -> dict:
    return {
        "accepted": False,
        "requestId": request_id,
        "targetProvider": target_provider,
        "effective": "immediate",
        "acceptedCallIds": [],
        "missingCallIds": list(missing_call_ids or []),
        "failures": [],
        "message": message,
    }


async def switch_paired_session_models(
    anchor_call_id: str,
    seat_id: str,
    target_provider: str,
    request_id: str,
) -> dict:
    # Resolve the opposite stream from either call leg and switch both sessions.
    anchor_call_id = str(anchor_call_id or "").strip()
    seat_id = str(seat_id or "").strip()
    target_provider = str(target_provider or "").strip().lower()
    request_id = str(request_id or "").strip()
    if (
        not anchor_call_id
        or not seat_id
        or target_provider not in VALID_PROVIDERS
        or not request_id
    ):
        return _paired_switch_rejection(
            request_id,
            target_provider,
            "INVALID_MODEL_SWITCH_COMMAND",
        )

    async with _session_lock:
        anchor_session = _active_sessions.get(anchor_call_id)
        if anchor_session is None or anchor_session.call_ended:
            return _paired_switch_rejection(
                request_id,
                target_provider,
                "CALL_NOT_FOUND",
                missing_call_ids=[anchor_call_id],
            )
        anchor_role = str(anchor_session.speaker or "").strip().lower()
        if anchor_role not in {"agent", "caller"}:
            return _paired_switch_rejection(
                request_id,
                target_provider,
                "CALL_ID_NOT_SWITCHABLE_STREAM",
            )
        if str(anchor_session.callto or "").strip() != seat_id:
            return _paired_switch_rejection(
                request_id,
                target_provider,
                "SEAT_ID_MISMATCH",
            )

        pair_key = (
            str(anchor_session.project or "").strip(),
            str(anchor_session.callfrom or "").strip(),
            str(anchor_session.callto or "").strip(),
        )
        peer_role = "caller" if anchor_role == "agent" else "agent"
        peer_sessions = [
            session
            for call_id, session in _active_sessions.items()
            if call_id != anchor_call_id
            and not session.call_ended
            and str(session.speaker or "").strip().lower() == peer_role
            and (
                str(session.project or "").strip(),
                str(session.callfrom or "").strip(),
                str(session.callto or "").strip(),
            ) == pair_key
        ]

    if not peer_sessions:
        return _paired_switch_rejection(
            request_id,
            target_provider,
            "PAIRED_STREAM_NOT_FOUND",
        )
    if len(peer_sessions) != 1:
        return _paired_switch_rejection(
            request_id,
            target_provider,
            "PAIRED_STREAM_AMBIGUOUS",
        )

    if anchor_role == "agent":
        agent_session, caller_session = anchor_session, peer_sessions[0]
    else:
        agent_session, caller_session = peer_sessions[0], anchor_session
    call_ids = [agent_session.call_id, caller_session.call_id]
    result = await switch_active_session_models(call_ids, target_provider, request_id)
    if set(result.get("acceptedCallIds") or []) != set(call_ids):
        result["accepted"] = False
        if result.get("message") == "ok":
            result["message"] = "PAIRED_MODEL_SWITCH_REJECTED"
    return result


async def switch_active_session_models(
    call_ids: list[str],
    target_provider: str,
    request_id: str,
) -> dict:
    """Apply one model switch request to all currently active stream sessions."""
    unique_call_ids = list(dict.fromkeys(
        str(call_id or "").strip() for call_id in call_ids if str(call_id or "").strip()
    ))
    async with _session_lock:
        sessions = {call_id: _active_sessions.get(call_id) for call_id in unique_call_ids}

    missing_call_ids: list[str] = []
    active_items: list[tuple[str, "BridgeSession"]] = []
    for call_id in unique_call_ids:
        session = sessions.get(call_id)
        if session is None or session.call_ended:
            missing_call_ids.append(call_id)
            continue
        active_items.append((call_id, session))

    results = await asyncio.gather(*(
        session.request_model_switch(target_provider, request_id)
        for _, session in active_items
    ))

    accepted_call_ids: list[str] = []
    failures: list[dict] = []
    for (call_id, _), result in zip(active_items, results):
        if result.get("accepted"):
            accepted_call_ids.append(call_id)
        else:
            failures.append({
                "callId": call_id,
                "message": result.get("message") or "MODEL_SWITCH_REJECTED",
            })

    accepted = bool(accepted_call_ids)
    return {
        "accepted": accepted,
        "requestId": request_id,
        "targetProvider": target_provider,
        "effective": "immediate",
        "acceptedCallIds": accepted_call_ids,
        "missingCallIds": missing_call_ids,
        "failures": failures,
        "message": "ok" if accepted else (failures[0]["message"] if failures else "NO_ACTIVE_CALLS"),
    }


async def force_end_all_sessions() -> int:
    """强制结束所有活跃通话会话。返回结束的会话数。"""
    async with _session_lock:
        sessions = list(_active_sessions.values())
        _active_sessions.clear()

    count = 0
    for session in sessions:
        try:
            await session.force_end()
            count += 1
        except Exception:
            LOG.exception("强制结束会话失败 callId=%s", session.call_id)
    LOG.info("强制结束 %d 个通话会话", count)
    return count


async def force_end_session_by_call_id(call_id: str) -> bool:
    """强制结束指定 callId 的会话。返回是否找到并结束。"""
    async with _session_lock:
        session = _active_sessions.pop(call_id, None)
    if session is None:
        return False
    try:
        await session.force_end()
        return True
    except Exception:
        LOG.exception("强制结束会话失败 callId=%s", call_id)
        return False


class BridgeSession:
    """一个通话会话的协议翻译。"""

    def __init__(
        self,
        client_ws,
        upstream_ws,
        call_id: str,
        callfrom: str = "micro",
        callto: str = "micro",
        project: str = "firebot",
        recording_store: RecordingStore | None = None,
        upstream_factory=None,
        xfyun_client=None,
        provider_factory=None,
        turn_recording_coordinator: TurnRecordingCoordinator | None = None,
        database_writer=None,
        hotword_manager: HotwordManager | None = None,
    ):
        self.client_ws = client_ws      # 外部公司 WebSocket (aiohttp WebSocketResponse)
        self.upstream_ws = upstream_ws  # 当前 FunASR 分段 WebSocket
        self._upstream_factory = upstream_factory
        self._upstream_sockets = [upstream_ws]
        self._reader_tasks: set[asyncio.Task] = set()
        self._providers: set = set()
        self._active_provider = None
        self._provider_factory = provider_factory or ASRProviderFactory(
            call_id=call_id,
            initial_funasr_ws=upstream_ws,
            funasr_connector=upstream_factory,
            xfyun_client=xfyun_client,
        )
        self.current_provider = _model_preference(call_id) or FUNASR
        self.pending_provider: str | None = None
        self._switch_request_id: str | None = None
        self._last_switch_request_id: str | None = None
        self._model_switch_lock = asyncio.Lock()
        self._audio_route_lock = asyncio.Lock()
        self._switch_task: asyncio.Task | None = None
        self._switch_audio_buffer = bytearray()
        self._switch_speech_ended = False
        self._switch_segment_id: str | None = None
        self._switch_previous_provider = FUNASR
        self._switch_started_at = 0.0
        self._switch_timeout_seconds = max(
            0.1,
            _env_float("ASR_MODEL_SWITCH_TIMEOUT_SECONDS", 15.0),
        )
        switch_buffer_seconds = max(
            0.1,
            _env_float("ASR_MODEL_SWITCH_BUFFER_SECONDS", 20.0),
        )
        self._switch_buffer_max_bytes = int(16000 * 2 * switch_buffer_seconds)
        self._retired_providers: set = set()
        self._failed_provider_segments: set[str] = set()
        self._recovery_started_segments: set[str] = set()
        self._segment_audio_cache: dict[str, bytes] = {}
        self._recovery_task: asyncio.Task | None = None
        self._running = False
        self._needs_new_upstream = False
        self.call_id = call_id
        self.callfrom = callfrom
        self.callto = callto
        self.project = normalize_asr_project(project, call_id=call_id)
        self._hotword_manager = hotword_manager or HotwordManager(project=self.project)
        self.speaker = "unknown"
        self.direction = "unknown"
        self.stream_id = "stream-main"
        self.out_seq = 0
        self.handshake_sent = False
        self.call_ended = False
        # 当前 speaker turn 的时间范围 (ms, 相对于通话开始)
        self.turn_start_ms = 0
        self.turn_end_ms = 0
        # ── VAD 配置：放宽静音确认，避免短暂停顿切碎同一句话 ──
        self._vad_use_raw_audio = _env_bool("ASR_VAD_USE_RAW_AUDIO", True)
        self._vad = VADEngine(VADConfig(
            frame_ms=20,
            vad_aggressiveness=_env_int("ASR_VAD_AGGRESSIVENESS", 3),
            speech_confirm_frames=_env_int("ASR_VAD_SPEECH_CONFIRM_FRAMES", 4),
            silence_confirm_ms=_env_int("ASR_VAD_SILENCE_CONFIRM_MS", 1300),
            min_speech_ms=_env_int("ASR_VAD_MIN_SPEECH_MS", 500),
            energy_silence_db=_env_float("ASR_VAD_ENERGY_SILENCE_DB", -42.0),
        ))
        self._recording_store = recording_store or create_recording_store()
        self._turn_recordings = turn_recording_coordinator or _turn_recording_coordinator
        self._database_writer = database_writer or _database_writer
        self._segment_seq = 0
        self.current_segment_id: str | None = None
        self._valid_segment_ids: set[str] = set()
        self._last_vad_telemetry_at = 0.0
        self._vad_telemetry_interval_sec = 0.2
        # ── 修复：VAD 静音看门狗 ──
        self._vad_watchdog_task: asyncio.Task | None = None
        self._last_audio_frame_at = 0.0
        self._vad_is_speaking = False
        self._audio_preprocessor = AudioPreprocessor()
        self._last_audio_gain_log_at: dict[str, float] = {}
        self._transcript_segments: dict[str, TranscriptTurn] = {}
        self._call_postprocessor = _call_postprocessor
        self._correction_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._completed_audio_save_tail: asyncio.Task | None = None
        self._latest_speech_events: dict[str, dict] = {}
        self._rabbitmq_published_segments: set[str] = set()
        self._corrected_turn_keys: set[tuple[str, ...]] = set()
        self._offline_segments: set[str] = set()
        self._pending_completed_texts: list[CompletedTurnRecording] = []
        self._address_scope_binding: AddressScopeBinding | None = None

    def set_address_scope_binding(self, binding: AddressScopeBinding) -> None:
        """Record the latest location handle for the later address-hotword stage."""
        self._address_scope_binding = binding

    def set_address_scope_hotwords(
        self, binding: AddressScopeBinding, hotwords: dict[str, int] | object
    ) -> bool:
        if not isinstance(hotwords, dict):
            return False
        self._address_scope_binding = binding
        changed = self._hotword_manager.set_address_hotwords(
            scope_id=binding.scope_id,
            inventory_version=binding.inventory_version,
            hotwords=hotwords,
        )
        return changed

    @property
    def address_scope_binding(self) -> AddressScopeBinding | None:
        return self._address_scope_binding

    async def _broadcast(self, event: dict, *, publish_to_rabbitmq: bool | None = None) -> None:
        event.setdefault("project", self.project)
        if publish_to_rabbitmq is None:
            await _broadcast_to_monitors(event)
        else:
            await _broadcast_to_monitors(event, publish_to_rabbitmq=publish_to_rabbitmq)

    def _available_providers(self) -> list[str]:
        available = getattr(self._provider_factory, "available_providers", None)
        if available is not None:
            return list(available())
        return [
            name for name in (FUNASR, XFYUN)
            if self._provider_factory.availability(name)[0]
        ]

    def model_state(self) -> dict:
        return {
            "event": "asr.model.state",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
            "speaker": self.speaker,
            "direction": self.direction,
            "currentProvider": self.current_provider,
            "pendingProvider": self.pending_provider,
            "requestId": self._switch_request_id,
            "availableProviders": self._available_providers(),
            "sendTimeMs": int(time.time() * 1000),
        }

    async def _broadcast_model_event(
        self,
        event_name: str,
        *,
        request_id: str | None = None,
        target_provider: str | None = None,
        error_code: str = "",
        message: str = "",
        fallback_provider: str = "",
        connect_elapsed_ms: int | None = None,
        buffered_audio_ms: int | None = None,
    ) -> None:
        event = self.model_state()
        event.update({
            "event": event_name,
            "requestId": request_id,
            "targetProvider": target_provider or self.pending_provider or self.current_provider,
            "effective": "immediate",
        })
        if error_code:
            event["errorCode"] = error_code
        if message:
            event["message"] = message
        if fallback_provider:
            event["fallbackProvider"] = fallback_provider
        if connect_elapsed_ms is not None:
            event["connectElapsedMs"] = max(0, int(connect_elapsed_ms))
        if buffered_audio_ms is not None:
            event["bufferedAudioMs"] = max(0, int(buffered_audio_ms))
        await self._broadcast(event, publish_to_rabbitmq=False)

    def _next_segment_id(self) -> str:
        self._segment_seq += 1
        speaker_name = self.speaker if self.speaker != "unknown" else "speaker"
        return f"{speaker_name}-{self._segment_seq:04d}"

    @staticmethod
    def _audio_duration_ms(pcm: bytes | bytearray) -> int:
        return int(len(pcm) * 1000 / 32000)

    def _register_provider(self, provider) -> None:
        self._providers.add(provider)
        provider_ws = getattr(provider, "ws", None)
        if provider_ws is not None:
            self.upstream_ws = provider_ws
            if provider_ws not in self._upstream_sockets:
                self._upstream_sockets.append(provider_ws)

    async def _finish_retired_provider(self, provider) -> None:
        try:
            await provider.finish()
        except Exception as exc:
            LOG.warning(
                "retired ASR provider finish failed callId=%s provider=%s segmentId=%s code=%s",
                self.call_id,
                getattr(provider, "name", "unknown"),
                getattr(provider, "segment_id", ""),
                getattr(exc, "code", type(exc).__name__),
            )

    async def _create_switch_provider(self, target_provider: str, segment_id: str):
        deadline = self._switch_started_at + self._switch_timeout_seconds
        last_error: Exception | None = None
        for _ in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(
                    self._provider_factory.create(
                        target_provider,
                        segment_id,
                        hotwords=self._hotword_manager.current_hotwords(),
                    ),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0)
        if isinstance(last_error, asyncio.TimeoutError):
            raise ProviderError(
                "MODEL_SWITCH_TIMEOUT",
                "目标 ASR 模型连接超时",
                provider=target_provider,
            ) from last_error
        if last_error is not None:
            raise last_error
        raise ProviderError(
            "MODEL_SWITCH_TIMEOUT",
            "目标 ASR 模型连接超时",
            provider=target_provider,
        )

    async def _activate_immediate_provider(
        self,
        provider,
        *,
        target_provider: str,
        request_id: str,
        segment_id: str,
    ) -> None:
        should_finish = False
        close_idle_provider = False
        buffered = b""
        async with self._audio_route_lock:
            if (
                self.call_ended
                or self.pending_provider != target_provider
                or self._switch_request_id != request_id
            ):
                await provider.close()
                return

            buffered = bytes(self._switch_audio_buffer)
            self._switch_audio_buffer.clear()
            should_finish = self._switch_speech_ended
            self._switch_speech_ended = False
            self._register_provider(provider)
            self._active_provider = provider
            self.handshake_sent = True
            self.current_provider = target_provider
            _remember_model_preference(self.call_id, target_provider)
            self.pending_provider = None
            self._last_switch_request_id = request_id
            self._switch_request_id = None
            self.current_segment_id = segment_id
            self._valid_segment_ids.add(segment_id)

            if buffered:
                await provider.send_audio(buffered)

            close_idle_provider = not buffered and not self._vad_is_speaking
            if close_idle_provider or should_finish:
                self._active_provider = None
                self.handshake_sent = False
                self._needs_new_upstream = target_provider == FUNASR
            elif self._running:
                self._start_provider_reader(provider)

        connect_elapsed_ms = int((time.monotonic() - self._switch_started_at) * 1000)
        buffered_audio_ms = self._audio_duration_ms(buffered)
        LOG.info(
            "ASR model changed immediately callId=%s requestId=%s provider=%s "
            "segmentId=%s connectElapsedMs=%s bufferedAudioMs=%s",
            self.call_id,
            request_id,
            target_provider,
            segment_id,
            connect_elapsed_ms,
            buffered_audio_ms,
        )
        await self._broadcast_model_event(
            "asr.model.changed",
            request_id=request_id,
            target_provider=target_provider,
            connect_elapsed_ms=connect_elapsed_ms,
            buffered_audio_ms=buffered_audio_ms,
        )

        if close_idle_provider:
            await provider.close()
            self._providers.discard(provider)
        elif should_finish:
            if self._running:
                self._start_provider_reader(provider)
            self._schedule_background_task(
                self._finish_retired_provider(provider),
                "finish-immediate-segment",
            )

    async def _fallback_immediate_switch(
        self,
        *,
        target_provider: str,
        request_id: str,
        segment_id: str,
        error: Exception,
    ) -> None:
        if self.call_ended:
            async with self._audio_route_lock:
                if self._switch_request_id == request_id:
                    self.pending_provider = None
                    self._switch_request_id = None
                    self._switch_audio_buffer.clear()
                    self._switch_speech_ended = False
            return
        fallback_provider = (
            FUNASR if target_provider != FUNASR else self._switch_previous_provider
        )
        error_code = str(getattr(error, "code", f"{target_provider.upper()}_START_FAILED"))
        error_message = str(getattr(error, "message", "目标 ASR 模型连接失败"))
        fallback = None
        try:
            fallback = await self._provider_factory.create(
                fallback_provider,
                segment_id,
                hotwords=self._hotword_manager.current_hotwords(),
            )
        except Exception:
            LOG.exception(
                "ASR immediate switch fallback failed callId=%s requestId=%s provider=%s",
                self.call_id,
                request_id,
                fallback_provider,
            )

        buffered = b""
        should_finish = False
        async with self._audio_route_lock:
            if self._switch_request_id != request_id:
                if fallback is not None:
                    await fallback.close()
                return
            buffered = bytes(self._switch_audio_buffer)
            self._switch_audio_buffer.clear()
            should_finish = self._switch_speech_ended
            self._switch_speech_ended = False
            self.pending_provider = None
            self._switch_request_id = None
            self._last_switch_request_id = request_id
            self.current_provider = fallback_provider
            _remember_model_preference(self.call_id, fallback_provider)

            if fallback is not None:
                self._register_provider(fallback)
                self._active_provider = fallback
                self.handshake_sent = True
                self.current_segment_id = segment_id
                self._valid_segment_ids.add(segment_id)
                if buffered:
                    await fallback.send_audio(buffered)
                if should_finish:
                    self._active_provider = None
                    self.handshake_sent = False
                elif self._running:
                    self._start_provider_reader(fallback)
            else:
                self._active_provider = None
                self.handshake_sent = False

        if fallback is not None and should_finish:
            if self._running:
                self._start_provider_reader(fallback)
            self._schedule_background_task(
                self._finish_retired_provider(fallback),
                "finish-switch-fallback-segment",
            )

        await self._broadcast_model_event(
            "asr.model.switch.failed",
            request_id=request_id,
            target_provider=target_provider,
            error_code=error_code,
            message=error_message,
            fallback_provider=fallback_provider,
            connect_elapsed_ms=int((time.monotonic() - self._switch_started_at) * 1000),
            buffered_audio_ms=self._audio_duration_ms(buffered),
        )

    async def _perform_immediate_switch(
        self,
        target_provider: str,
        request_id: str,
        segment_id: str,
    ) -> None:
        try:
            provider = await self._create_switch_provider(target_provider, segment_id)
            await self._activate_immediate_provider(
                provider,
                target_provider=target_provider,
                request_id=request_id,
                segment_id=segment_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fallback_immediate_switch(
                target_provider=target_provider,
                request_id=request_id,
                segment_id=segment_id,
                error=exc,
            )

    async def _abort_immediate_switch_for_buffer_limit(
        self,
        *,
        target_provider: str,
        request_id: str,
        segment_id: str,
    ) -> None:
        switch_task = self._switch_task
        if switch_task is not None and not switch_task.done():
            switch_task.cancel()
            await asyncio.gather(switch_task, return_exceptions=True)
        await self._fallback_immediate_switch(
            target_provider=target_provider,
            request_id=request_id,
            segment_id=segment_id,
            error=ProviderError(
                "MODEL_SWITCH_BUFFER_LIMIT",
                "模型连接期间音频缓存达到上限，已回退 FunASR",
                provider=target_provider,
            ),
        )

    async def request_model_switch(self, target_provider: str, request_id: str) -> dict:
        target_provider = str(target_provider or "").strip().lower()
        request_id = str(request_id or "").strip()
        if target_provider not in VALID_PROVIDERS or not request_id:
            return {"accepted": False, "message": "INVALID_MODEL_SWITCH_COMMAND"}
        if self.call_ended:
            return {"accepted": False, "message": "CALL_ALREADY_ENDED"}

        same_provider = False
        segment_id: str | None = None
        async with self._model_switch_lock:
            available, reason = self._provider_factory.availability(target_provider)
            if not available:
                return {"accepted": False, "message": reason or "PROVIDER_UNAVAILABLE"}
            if self.pending_provider:
                if self.pending_provider == target_provider and self._switch_request_id == request_id:
                    return {"accepted": True, "changed": False, "state": self.model_state()}
                return {"accepted": False, "message": "MODEL_SWITCH_IN_PROGRESS"}
            if self.current_provider == target_provider:
                same_provider = True
                _remember_model_preference(self.call_id, target_provider)
            else:
                self.pending_provider = target_provider
                self._switch_request_id = request_id
                self._switch_previous_provider = self.current_provider
                self._switch_started_at = time.monotonic()
                self._switch_audio_buffer.clear()
                self._switch_speech_ended = False
                segment_id = self._next_segment_id()
                self._switch_segment_id = segment_id
                self.current_segment_id = segment_id
                self._valid_segment_ids.add(segment_id)

                old_provider = self._active_provider
                self._active_provider = None
                self.handshake_sent = False
                if old_provider is not None:
                    self._retired_providers.add(old_provider)
                    self._schedule_background_task(
                        self._finish_retired_provider(old_provider),
                        "finish-provider-at-model-switch",
                    )

        if same_provider:
            await self._broadcast_model_event(
                "asr.model.state",
                request_id=request_id,
                target_provider=target_provider,
                connect_elapsed_ms=0,
                buffered_audio_ms=0,
            )
            return {"accepted": True, "changed": False, "state": self.model_state()}

        LOG.info(
            "ASR immediate model switch pending callId=%s requestId=%s current=%s target=%s",
            self.call_id,
            request_id,
            self._switch_previous_provider,
            target_provider,
        )
        await self._broadcast_model_event(
            "asr.model.switch.pending",
            request_id=request_id,
            target_provider=target_provider,
            connect_elapsed_ms=0,
            buffered_audio_ms=0,
        )
        async with self._model_switch_lock:
            if (
                segment_id is not None
                and self.pending_provider == target_provider
                and self._switch_request_id == request_id
            ):
                self._switch_task = asyncio.create_task(
                    self._perform_immediate_switch(
                        target_provider,
                        request_id,
                        segment_id,
                    )
                )
        return {"accepted": True, "changed": True, "state": self.model_state()}

    async def run(self, first_msg=None) -> None:
        self._first_msg = first_msg
        self._running = True
        await register_session(self)
        self._client_task = asyncio.create_task(self._client_to_upstream())
        # ── 修复：启动 VAD 静音看门狗 ──
        self._vad_watchdog_task = asyncio.create_task(self._vad_watchdog())

        try:
            await self._client_task
            if self._reader_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*list(self._reader_tasks), return_exceptions=True),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    LOG.debug("等待 FunASR 分段最终结果超时, callId=%s", self.call_id)
        finally:
            self._running = False
            if self._switch_task is not None and not self._switch_task.done():
                self._switch_task.cancel()
                await asyncio.gather(self._switch_task, return_exceptions=True)
            if self._vad_watchdog_task and not self._vad_watchdog_task.done():
                self._vad_watchdog_task.cancel()
            for task in list(self._reader_tasks):
                if not task.done():
                    task.cancel()
            for provider in list(self._providers):
                try:
                    await provider.close()
                except Exception:
                    pass
            close_unused = getattr(self._provider_factory, "close_unused", None)
            if close_unused is not None:
                try:
                    await close_unused()
                except Exception:
                    pass
            for ws in self._upstream_sockets:
                try:
                    if not getattr(ws, "closed", False):
                        await ws.close()
                except Exception:
                    pass
            unregistered = await unregister_session(self.call_id, self)
            if unregistered:
                if self.call_ended:
                    _forget_model_preference(self.call_id)
                _purge_global_call_state(self.call_id)

    async def force_end(self, *, preserve_call_state: bool = False) -> None:
        """强制结束本会话 —— 广播 call.ended 并关闭所有连接。"""
        self._running = False
        self.call_ended = True
        if not preserve_call_state:
            _forget_model_preference(self.call_id)
        if self._switch_task is not None and not self._switch_task.done():
            self._switch_task.cancel()
            await asyncio.gather(self._switch_task, return_exceptions=True)
        self._switch_audio_buffer.clear()
        # 广播 call.ended 给前端监控
        await self._broadcast({
            "event": "call.ended",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
        }, publish_to_rabbitmq=False)
        # 关闭所有上游连接
        for provider in list(self._providers):
            try:
                await provider.close()
            except Exception:
                pass
        for ws in self._upstream_sockets:
            try:
                if not getattr(ws, "closed", False):
                    await ws.close()
            except Exception:
                pass
        # 关闭客户端连接
        try:
            if not getattr(self.client_ws, "closed", False):
                await self.client_ws.close()
        except Exception:
            pass
        # 取消后台任务
        for task_name in ("_client_task", "_vad_watchdog_task"):
            task = getattr(self, task_name, None)
            if task and not task.done():
                task.cancel()
        for task in list(self._reader_tasks):
            if not task.done():
                task.cancel()
        if not preserve_call_state:
            _purge_global_call_state(self.call_id)
        LOG.info("强制结束会话 callId=%s", self.call_id)

    def _start_upstream_reader(self, ws) -> None:
        task = asyncio.create_task(self._upstream_to_client(ws, close_after_offline=True))
        self._reader_tasks.add(task)
        task.add_done_callback(self._reader_tasks.discard)

    async def _connect_fresh_upstream(self) -> None:
        if self._upstream_factory is None:
            raise RuntimeError("fresh FunASR connection requested without upstream_factory")
        ws = await self._upstream_factory()
        self.upstream_ws = ws
        self._upstream_sockets.append(ws)
        self._needs_new_upstream = False
        if self._running:
            self._start_upstream_reader(ws)

    def _start_provider_reader(self, provider) -> None:
        task = asyncio.create_task(self._provider_to_client(provider))
        self._reader_tasks.add(task)
        task.add_done_callback(self._reader_tasks.discard)

    async def _start_provider_segment(
        self,
        provider_name: str | None = None,
        *,
        segment_id: str | None = None,
    ):
        if self._recovery_task is not None and not self._recovery_task.done():
            await self._recovery_task

        changed_event: tuple[str, str] | None = None
        failure_event: tuple[str | None, str, str, str] | None = None
        async with self._model_switch_lock:
            requested_provider = provider_name or self.pending_provider or self.current_provider
            previous_provider = self.current_provider
            pending_matches = bool(self.pending_provider and requested_provider == self.pending_provider)
            request_id = self._switch_request_id if pending_matches else self._last_switch_request_id

            if segment_id is None:
                self._segment_seq += 1
                speaker_name = self.speaker if self.speaker != "unknown" else "speaker"
                segment_id = f"{speaker_name}-{self._segment_seq:04d}"
            self.current_segment_id = segment_id
            self._valid_segment_ids.add(segment_id)

            try:
                provider = await self._provider_factory.create(
                    requested_provider,
                    segment_id,
                    hotwords=self._hotword_manager.current_hotwords(),
                )
                effective_provider = requested_provider
            except Exception as exc:
                error_code = str(getattr(exc, "code", f"{requested_provider.upper()}_START_FAILED"))
                error_message = str(getattr(exc, "message", "目标 ASR 模型连接失败"))
                fallback_provider = FUNASR if requested_provider != FUNASR else previous_provider
                if fallback_provider == requested_provider:
                    if pending_matches:
                        self.pending_provider = None
                        self._switch_request_id = None
                    failure_event = (request_id, requested_provider, error_code, error_message)
                    provider = None
                    effective_provider = previous_provider
                else:
                    try:
                        provider = await self._provider_factory.create(
                            fallback_provider,
                            segment_id,
                            hotwords=self._hotword_manager.current_hotwords(),
                        )
                        effective_provider = fallback_provider
                    except Exception:
                        if pending_matches:
                            self.pending_provider = None
                            self._switch_request_id = None
                        failure_event = (request_id, requested_provider, error_code, error_message)
                        provider = None
                        effective_provider = previous_provider
                    else:
                        if pending_matches:
                            self.current_provider = fallback_provider
                            _remember_model_preference(self.call_id, fallback_provider)
                            self.pending_provider = None
                            self._switch_request_id = None
                            failure_event = (request_id, requested_provider, error_code, error_message)
                        else:
                            LOG.warning(
                                "ASR selected provider unavailable for one segment; "
                                "use fallback and retry selected provider next segment "
                                "callId=%s provider=%s segmentId=%s code=%s fallback=%s",
                                self.call_id,
                                requested_provider,
                                segment_id,
                                error_code,
                                fallback_provider,
                            )

            if provider is None:
                self.handshake_sent = False
            else:
                self._active_provider = provider
                self._providers.add(provider)
                self.handshake_sent = True
                provider_ws = getattr(provider, "ws", None)
                if provider_ws is not None:
                    self.upstream_ws = provider_ws
                    if provider_ws not in self._upstream_sockets:
                        self._upstream_sockets.append(provider_ws)
                if pending_matches and effective_provider == requested_provider:
                    self.current_provider = requested_provider
                    _remember_model_preference(self.call_id, requested_provider)
                    self.pending_provider = None
                    self._last_switch_request_id = request_id
                    self._switch_request_id = None
                    changed_event = (request_id or "", requested_provider)
                if self._running:
                    self._start_provider_reader(provider)

        if failure_event is not None:
            req, target, code, message = failure_event
            await self._broadcast_model_event(
                "asr.model.switch.failed",
                request_id=req,
                target_provider=target,
                error_code=code,
                message=message,
                fallback_provider=self.current_provider,
            )
        if changed_event is not None:
            req, target = changed_event
            LOG.info(
                "ASR model changed callId=%s requestId=%s provider=%s segmentId=%s",
                self.call_id,
                req,
                target,
                segment_id,
            )
            await self._broadcast_model_event(
                "asr.model.changed",
                request_id=req,
                target_provider=target,
            )
        if provider is None:
            raise ProviderError(
                "ASR_PROVIDER_START_FAILED",
                "ASR 模型和回退模型均连接失败",
                provider=requested_provider,
            )
        return provider

    async def _finish_current_provider(self, audio_segment: bytes | None = None) -> None:
        provider = self._active_provider
        if provider is None:
            if self.handshake_sent:
                try:
                    await self.upstream_ws.send_str(json.dumps({
                        "is_speaking": False,
                        "mode": "2pass",
                    }, ensure_ascii=False))
                except Exception:
                    pass
            self.handshake_sent = False
            self._needs_new_upstream = True
            return

        self._active_provider = None
        self.handshake_sent = False
        self._needs_new_upstream = provider.name == FUNASR
        try:
            await provider.finish()
        except ProviderError as exc:
            await self._handle_provider_failure(provider, ProviderResult(
                provider=provider.name,
                segment_id=provider.segment_id,
                error_code=exc.code,
                error_message=exc.message,
            ))
        if audio_segment and provider.segment_id in self._failed_provider_segments:
            await self._recover_failed_xfyun_segment(provider.segment_id, audio_segment)

    async def _provider_to_client(self, provider) -> None:
        try:
            async for result in provider.events():
                if result.failed:
                    await self._handle_provider_failure(provider, result)
                    break
                await self._handle_provider_result(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception(
                "ASR provider result handler failed callId=%s provider=%s segmentId=%s",
                self.call_id,
                getattr(provider, "name", "unknown"),
                getattr(provider, "segment_id", ""),
            )
        finally:
            try:
                await provider.close()
            except Exception:
                pass
            self._providers.discard(provider)
            self._retired_providers.discard(provider)

    async def _handle_provider_result(self, result: ProviderResult) -> None:
        text = _normalize_asr_text(result.text)
        segment_id = result.segment_id or self.current_segment_id
        if not text:
            return
        if segment_id and segment_id not in self._valid_segment_ids:
            LOG.debug(
                "忽略无 VAD 语音的 ASR 文本 callId=%s provider=%s segmentId=%s",
                self.call_id,
                result.provider,
                segment_id,
            )
            return
        if self._hotword_manager.update_from_recognized_text(text):
            LOG.info(
                "hotword stage inferred callId=%s stage=%s text=%s",
                self.call_id,
                self._hotword_manager.stage.value,
                text[:80],
            )

        await self._broadcast_speech_to_monitors(
            text,
            segment_id=segment_id,
            provider=result.provider,
            publish_to_rabbitmq=False,
        )
        if not result.is_final:
            try:
                await self._send_streaming_text(text, segment_id=segment_id, mode=result.mode)
            except ClientConnectionResetError:
                LOG.info("客户端已断连，跳过 streaming text, callId=%s", self.call_id)
            return

        if segment_id:
            self._offline_segments.add(segment_id)
            self._segment_audio_cache.pop(segment_id, None)
        try:
            await self._send_speech_final(
                text,
                segment_id=segment_id,
                provider=result.provider,
            )
        except ClientConnectionResetError:
            LOG.info("客户端已断连，跳过 speech.final, callId=%s", self.call_id)
        LOG.info(
            "speech.final callId=%s provider=%s segmentId=%s text=%s",
            self.call_id,
            result.provider,
            segment_id,
            text[:80],
        )

    async def _handle_provider_failure(self, provider, result: ProviderResult) -> None:
        segment_id = result.segment_id or getattr(provider, "segment_id", "")
        if self.call_ended:
            LOG.info(
                "ignore ASR provider failure after call ended "
                "callId=%s provider=%s segmentId=%s code=%s",
                self.call_id,
                result.provider,
                segment_id,
                result.error_code,
            )
            return
        if provider in self._retired_providers:
            LOG.info(
                "ignore retired ASR provider failure callId=%s provider=%s segmentId=%s code=%s",
                self.call_id,
                result.provider,
                segment_id,
                result.error_code,
            )
            return
        if segment_id in self._failed_provider_segments:
            return
        self._failed_provider_segments.add(segment_id)
        fallback = self.current_provider
        if result.provider == XFYUN:
            fallback = FUNASR
            LOG.warning(
                "ASR provider failed for one segment; preserve selected model and retry "
                "callId=%s provider=%s segmentId=%s code=%s segmentFallback=%s",
                self.call_id,
                result.provider,
                segment_id,
                result.error_code,
                fallback,
            )
        else:
            LOG.warning(
                "ASR provider failed callId=%s provider=%s segmentId=%s code=%s fallback=%s",
                self.call_id,
                result.provider,
                segment_id,
                result.error_code,
                fallback,
            )
            await self._broadcast_model_event(
                "asr.model.switch.failed",
                request_id=self._last_switch_request_id,
                target_provider=result.provider,
                error_code=result.error_code or "ASR_PROVIDER_FAILED",
                message=result.error_message or "ASR 模型识别失败",
                fallback_provider=fallback,
            )
        cached_audio = self._segment_audio_cache.get(segment_id)
        if result.provider == XFYUN and cached_audio:
            await self._recover_failed_xfyun_segment(segment_id, cached_audio)

    async def _recover_failed_xfyun_segment(self, segment_id: str, pcm: bytes) -> None:
        if not segment_id or not pcm or segment_id in self._recovery_started_segments:
            return
        self._recovery_started_segments.add(segment_id)
        LOG.info("replay failed Xfyun segment with FunASR callId=%s segmentId=%s", self.call_id, segment_id)
        try:
            provider = await self._provider_factory.create(
                FUNASR,
                segment_id,
                hotwords=self._hotword_manager.current_hotwords(),
            )
            self._providers.add(provider)
            provider_ws = getattr(provider, "ws", None)
            if provider_ws is not None and provider_ws not in self._upstream_sockets:
                self._upstream_sockets.append(provider_ws)
            if self._running:
                self._start_provider_reader(provider)
            await provider.send_audio(pcm)
            await provider.finish()
        except Exception:
            LOG.exception("FunASR replay failed callId=%s segmentId=%s", self.call_id, segment_id)

    # ── 客户端 → FunASR ────────────────────────────────────────────

    async def _client_to_upstream(self) -> None:
        try:
            # 先处理 gateway 预取的首条消息
            if self._first_msg is not None:
                msg = self._first_msg
                self._first_msg = None
                if await self._dispatch_client_msg(msg):
                    return

            async for msg in self.client_ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
                if msg.type != WSMsgType.TEXT:
                    continue
                if await self._dispatch_client_msg(msg):
                    return
        except Exception:
            LOG.exception("客户端上行处理异常, callId=%s", self.call_id)
            if not self.call_ended:
                await self._stop_funasr()

    async def _dispatch_client_msg(self, msg) -> bool:
        """处理一条客户端消息，返回 True 表示会话结束。"""
        try:
            event = json.loads(msg.data)
        except json.JSONDecodeError:
            await self._ack(None, False, "INVALID_JSON")
            return False

        event_type = event.get("eventType") or event.get("type") or ""

        if event_type == "call.started":
            await self._on_call_started(event)
        elif event_type == "audio.frame":
            await self._on_audio_frame(event)
        elif event_type == "call.ended":
            await self._on_call_ended(event)
            return True
        elif event_type == "scene_signal.add":
            try:
                changed = self._hotword_manager.apply_event(event)
            except InvalidSceneSignal as exc:
                LOG.warning(
                    "scene hotword signal rejected callId=%s error=%s",
                    self.call_id,
                    exc,
                )
                await self._ack(
                    event.get("seq"), False, "INVALID_SCENE_SIGNAL"
                )
                return False
            LOG.info(
                "scene hotwords queued callId=%s version=%d libraries=%s "
                "count=%d changed=%s warning=%s truncated=%s "
                "effective_from=next_segment",
                self.call_id,
                self._hotword_manager.hotword_version,
                ",".join(self._hotword_manager.library_ids),
                self._hotword_manager.hotword_count,
                changed,
                self._hotword_manager.warning_threshold_reached,
                self._hotword_manager.truncated,
            )
            await self._ack(event.get("seq"), True, "ok")
        elif event_type in {"stage.changed", "asr.hotwords.switch"}:
            changed = self._hotword_manager.apply_event(event)
            LOG.info(
                "hotword switch event callId=%s eventType=%s stage=%s changed=%s",
                self.call_id,
                event_type,
                self._hotword_manager.stage.value,
                changed,
            )
            await self._ack(event.get("seq"), True, "ok")
        else:
            await self._ack(event.get("seq"), True, "ok")
        return False

    async def _on_call_started(self, event: dict) -> None:
        self.stream_id = event.get("streamId", "stream-main")
        self.callfrom = event.get("callfrom", "micro")
        self.callto = event.get("callto", "micro")
        LOG.info("call.started callId=%s callfrom=%s callto=%s streamId=%s",
                 self.call_id, self.callfrom, self.callto, self.stream_id)
        await self._ack(event.get("seq"), True, "ok")
        await self._broadcast({
            "event": "call.started",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
            "streamId": self.stream_id,
        })
        await self._broadcast_model_event("asr.model.state")

    async def _on_audio_frame(self, event: dict) -> None:
        self._last_audio_frame_at = time.time()  # ── 修复：记录最后音频帧时间供看门狗使用
        seq = event.get("seq")
        payload = event.get("payload", {})
        b64 = payload.get("audioBase64", "")

        if not b64:
            await self._ack(seq, False, "MISSING_REQUIRED_FIELD")
            return

        try:
            pcm = base64.b64decode(b64)
        except Exception:
            await self._ack(seq, False, "INVALID_JSON")
            return

        # 从 payload 中提取会话元信息（每条音频帧都带）
        cf = payload.get("callfrom", "")
        ct = payload.get("callto", "")
        sp = payload.get("speaker", "")
        dr = payload.get("direction", "")
        if cf:
            self.callfrom = cf
        if ct:
            self.callto = ct

        start_ms = int(payload.get("startTimeMs", 0))
        end_ms = int(payload.get("endTimeMs", 0))

        if sp:
            if sp != self.speaker and self.handshake_sent:
                # 说话人切换：只结束上一轮；下一轮等待 VAD 确认有人声后再启动。
                LOG.info("speaker switch: %s → %s, callId=%s", self.speaker, sp, self.call_id)
                await self._stop_funasr()
                self.turn_start_ms = start_ms
                self.turn_end_ms = end_ms
                self.speaker = sp
            elif sp != self.speaker:
                # 首次设置 speaker（handshake 尚未发送）
                self.turn_start_ms = start_ms
                self.turn_end_ms = end_ms
                self.speaker = sp
            else:
                # 同一说话人继续：扩展 endTime
                if end_ms > self.turn_end_ms:
                    self.turn_end_ms = end_ms
        if dr:
            self.direction = dr

        if self._is_hold_active():
            LOG.info(
                "call held, skip audio frame callId=%s callfrom=%s callto=%s speaker=%s seq=%s",
                self.call_id,
                self.callfrom,
                self.callto,
                self.speaker,
                seq,
            )
            await self._ack(seq, True, "CALL_HELD")
            return

        raw_pcm = pcm
        audio = self._audio_preprocessor.process(
            self.call_id,
            self.speaker,
            pcm,
            seq=seq if isinstance(seq, int) else None,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
        )
        self._log_audio_process_result(audio)
        pcm = audio.pcm
        vad_pcm = raw_pcm if self._vad_use_raw_audio else pcm

        vad_result = await self._process_vad_frame(vad_pcm, stop_funasr_on_speech_end=False)
        should_send_to_asr = bool(
            vad_result
            and (vad_result.speech_started or vad_result.is_speaking or vad_result.speech_ended)
        )

        switch_buffer_limit: tuple[str, str, str] | None = None
        if should_send_to_asr:
            async with self._audio_route_lock:
                if self.pending_provider:
                    self._switch_audio_buffer.extend(pcm)
                    if len(self._switch_audio_buffer) > self._switch_buffer_max_bytes:
                        LOG.warning(
                            "ASR model switch buffer exceeded expected bound "
                            "callId=%s requestId=%s bytes=%s maxBytes=%s",
                            self.call_id,
                            self._switch_request_id,
                            len(self._switch_audio_buffer),
                            self._switch_buffer_max_bytes,
                        )
                        if self._switch_request_id and self._switch_segment_id:
                            switch_buffer_limit = (
                                self.pending_provider,
                                self._switch_request_id,
                                self._switch_segment_id,
                            )
                    if vad_result.speech_ended:
                        self._switch_speech_ended = True
                else:
                    if not self.handshake_sent:
                        await self._start_provider_segment()

                    try:
                        await self._active_provider.send_audio(pcm)
                    except ProviderError as exc:
                        provider = self._active_provider
                        if provider is not None:
                            await self._handle_provider_failure(provider, ProviderResult(
                                provider=provider.name,
                                segment_id=provider.segment_id,
                                error_code=exc.code,
                                error_message=exc.message,
                            ))

                    if vad_result.speech_ended:
                        await self._finish_current_provider(vad_result.audio_segment)

        if switch_buffer_limit is not None:
            target_provider, request_id, segment_id = switch_buffer_limit
            await self._abort_immediate_switch_for_buffer_limit(
                target_provider=target_provider,
                request_id=request_id,
                segment_id=segment_id,
            )

        await self._ack(seq, True, "ok")

    def _log_audio_process_result(self, audio) -> None:
        for diagnostic in audio.diagnostics:
            LOG.warning(
                "audio diagnostics callId=%s speaker=%s %s raw=%.1fdB processed=%.1fdB gain=%.1fdB",
                self.call_id,
                self.speaker,
                diagnostic,
                audio.raw_db,
                audio.processed_db,
                audio.gain_db,
            )

        if audio.gain_db <= 0:
            return
        key = f"{self.call_id}:{self.speaker}"
        now = time.time()
        if now - self._last_audio_gain_log_at.get(key, 0.0) < 2.0:
            return
        self._last_audio_gain_log_at[key] = now
        LOG.info(
            "audio gain applied callId=%s speaker=%s raw=%.1fdB processed=%.1fdB gain=%.1fdB level=%s",
            self.call_id,
            self.speaker,
            audio.raw_db,
            audio.processed_db,
            audio.gain_db,
            audio.audio_level,
        )

    # ── 修复：VAD 静音看门狗 ────────────────────────────────────────
    async def _vad_watchdog(self) -> None:
        """后台定时喂静音帧给 VAD。

        当一方停止说话后可能不再有音频帧到达，VAD 内部状态机会卡住。
        此协程每 200ms 检查：若无新音频帧 → 喂一帧静音 → VAD 推进 →
        累积足够静音 → 检测 speech_ended → 广播 silence → 前端切分。
        """
        silence_frame = b'\x00' * 640  # 20ms 16kHz 16bit 静音
        while self._running:
            await asyncio.sleep(0.2)
            if not self._running:
                break
            if not self.handshake_sent:
                continue
            if self._last_audio_frame_at > 0 and time.time() - self._last_audio_frame_at < 0.2:
                continue  # 最近有音频帧，VAD 由 _on_audio_frame 正常驱动
            # 喂静音推进 VAD 状态
            await self._process_vad_frame(silence_frame)
            # 发送 telemetry（如有变化）
            await self._send_vad_telemetry_if_due()

    async def _send_vad_telemetry_if_due(self) -> None:
        now = time.time()
        if now - self._last_vad_telemetry_at < self._vad_telemetry_interval_sec:
            return
        self._last_vad_telemetry_at = now
        vad_state = "speaking" if self._vad_is_speaking else "silence"
        volume_db, audio_level = self._pcm_volume(b'\x00' * 640)
        await self._broadcast_vad_event(vad_state, 0, volume_db, audio_level)

    def _track_background_task(self, task: asyncio.Task, label: str) -> None:
        self._background_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.exception("后台任务异常, label=%s callId=%s", label, self.call_id)

        task.add_done_callback(_done)

    def _schedule_background_task(self, coro, label: str) -> None:
        self._track_background_task(asyncio.create_task(coro), label)

    def _schedule_completed_audio_save(self, completed: CompletedTurnRecording) -> None:
        previous = self._completed_audio_save_tail

        async def _ordered_save() -> None:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            await self._save_and_broadcast_completed_audio(completed)

        task = asyncio.create_task(_ordered_save())
        self._completed_audio_save_tail = task
        self._track_background_task(task, "save_completed_audio")

    def _schedule_remember_speech(self, event: dict) -> None:
        self._schedule_background_task(
            asyncio.to_thread(self._database_writer.remember_speech, dict(event)),
            "remember_speech",
        )

    async def _drain_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_vad_frame(self, pcm: bytes, *, stop_funasr_on_speech_end: bool = True):
        try:
            vad_result = self._vad.feed(pcm)
        except Exception:
            LOG.exception("VAD 处理异常, callId=%s", self.call_id)
            return None

        self._vad_is_speaking = vad_result.is_speaking  # ── 修复：记录给看门狗 telemetry 使用
        volume_db, audio_level = self._pcm_volume(pcm)
        now = time.time()

        if vad_result.speech_started:
            if self.current_segment_id:
                self._valid_segment_ids.add(self.current_segment_id)
            self._turn_recordings.mark_speech_started(
                call_id=self.call_id,
                callfrom=self.callfrom,
                callto=self.callto,
                speaker=self.speaker,
            )
            # 对方 VAD 已确认开始说话，说明上一方的 turn 可以在其 VAD 结束后完成。
            # 这个信号比等待对方 ASR 文本更早，避免多轮短问答被合并成一个 turn。
            completed_recordings = self._turn_recordings.mark_effective_speech(
                call_id=self.call_id,
                callfrom=self.callfrom,
                callto=self.callto,
                speaker=self.speaker,
            )
            for item in completed_recordings:
                await self._publish_or_defer_completed_turn_text(item)
                self._schedule_completed_audio_save(item)
            await self._broadcast_vad_event("speaking", vad_result.silence_duration_ms, volume_db, audio_level)
            self._last_vad_telemetry_at = now
        if vad_result.speech_ended:
            segment_id = self.current_segment_id
            if segment_id:
                self._valid_segment_ids.add(segment_id)
            if vad_result.audio_segment:
                if segment_id:
                    self._segment_audio_cache[segment_id] = vad_result.audio_segment
                completed = self._turn_recordings.append_vad_segment(self._recording_chunk(
                    vad_result.audio_segment,
                    segment_id=segment_id,
                ))
                for item in completed:
                    await self._publish_or_defer_completed_turn_text(item)
                    self._schedule_completed_audio_save(item)
            await self._broadcast_vad_event("ended", vad_result.silence_duration_ms, volume_db, audio_level)
            if stop_funasr_on_speech_end:
                await self._finish_current_provider(vad_result.audio_segment)
            self._last_vad_telemetry_at = now
        elif now - self._last_vad_telemetry_at >= self._vad_telemetry_interval_sec:
            vad_state = "speaking" if vad_result.is_speaking else "silence"
            await self._broadcast_vad_event(vad_state, vad_result.silence_duration_ms, volume_db, audio_level)
            self._last_vad_telemetry_at = now
        return vad_result

    def _recording_chunk(self, pcm: bytes, segment_id: str | None = None) -> RecordingChunk:
        return RecordingChunk(
            call_id=self.call_id,
            callfrom=self.callfrom,
            callto=self.callto,
            speaker=self.speaker,
            direction=self.direction,
            segment_id=segment_id or self.current_segment_id,
            pcm=pcm,
            start_time_ms=self.turn_start_ms,
            end_time_ms=self.turn_end_ms,
        )

    # ── 修复：远程上传获取 record_id → 入库；本地存储 → 前端播放 ──
    async def _save_and_broadcast_completed_audio(self, completed: CompletedTurnRecording) -> None:
        if self._is_hold_active(call_id=completed.call_id, callfrom=completed.callfrom, callto=completed.callto):
            LOG.info("call held, skip audio.segment callId=%s speaker=%s", completed.call_id, completed.speaker)
            return

        record_id = None
        audio_url = ""
        local_audio_url = ""
        audio_start_ms = completed.start_time_ms
        audio_end_ms = completed.end_time_ms
        audio_duration_ms = 0

        # 1. 远程上传 → 获取 record_id / 文件服务播放地址。audioUrl 对外只使用文件服务地址。
        try:
            remote_saved = await asyncio.to_thread(
                self._recording_store.save_segment,
                completed.pcm,
                call_id=completed.call_id,
                speaker=completed.speaker,
                start_time_ms=completed.start_time_ms,
                end_time_ms=completed.end_time_ms,
            )
            record_id = getattr(remote_saved, "record_id", None)
            audio_url = getattr(remote_saved, "audio_url", "") or ""
            audio_duration_ms = getattr(remote_saved, "duration_ms", 0) or audio_duration_ms
            audio_start_ms = getattr(remote_saved, "start_time_ms", audio_start_ms)
            audio_end_ms = getattr(remote_saved, "end_time_ms", audio_end_ms)
            LOG.info("远程录音上传成功 callId=%s record_id=%s audioUrl=%s", completed.call_id, record_id, audio_url)
        except Exception:
            LOG.exception("远程录音上传失败, callId=%s speaker=%s",
                          completed.call_id, completed.speaker)

        # 2. 本地存储 → 前端 HTTPS 播放
        try:
            _local_store = RecordingStore()
            local_saved = await asyncio.to_thread(
                _local_store.save_segment,
                completed.pcm,
                call_id=completed.call_id,
                speaker=completed.speaker,
                start_time_ms=completed.start_time_ms,
                end_time_ms=completed.end_time_ms,
            )
            local_audio_url = local_saved.audio_url
            audio_start_ms = local_saved.start_time_ms
            audio_end_ms = local_saved.end_time_ms
            audio_duration_ms = local_saved.duration_ms
        except Exception:
            LOG.exception("本地录音存储失败, callId=%s speaker=%s",
                          completed.call_id, completed.speaker)

        if not audio_url:
            LOG.warning(
                "远程录音地址为空，audio.segment.audioUrl 不回退本地地址, callId=%s speaker=%s",
                completed.call_id, completed.speaker,
            )

        audio_event = {
            "event": "audio.segment",
            "callId": completed.call_id,
            "segmentId": completed.segment_id,
            "segmentIds": list(completed.segment_ids),
            "recordId": record_id,
            "callfrom": completed.callfrom,
            "callto": completed.callto,
            "speaker": completed.speaker,
            "direction": completed.direction,
            "audioUrl": audio_url,                  # 文件服务 URL → RabbitMQ / DB
            "localAudioUrl": local_audio_url,       # 本地 URL → 前端 HTTPS 播放
            "audioDurationMs": audio_duration_ms,
            "startTimeMs": audio_start_ms,
            "endTimeMs": audio_end_ms,
            "sendTimeMs": int(time.time() * 1000),
        }
        try:
            await asyncio.to_thread(self._database_writer.save_audio_turn, audio_event)
        except Exception:
            LOG.exception("数据库写入 audio_turn 失败, callId=%s", completed.call_id)
        await self._broadcast(audio_event)

    def _pcm_volume(self, pcm: bytes) -> tuple[float, int]:
        if len(pcm) < 2:
            return -60.0, 0

        sample_count = len(pcm) // 2
        total = 0
        for i in range(0, sample_count * 2, 2):
            sample = int.from_bytes(pcm[i:i + 2], byteorder="little", signed=True)
            total += sample * sample

        if total <= 0:
            return -60.0, 0

        rms = math.sqrt(total / sample_count)
        db = max(-60.0, min(0.0, 20 * math.log10(rms / 32768.0)))
        level = int(round((db + 60.0) * 100.0 / 60.0))
        return round(db, 1), max(0, min(100, level))

    async def _broadcast_vad_event(self, vad_state: str, silence_duration_ms: int = 0, volume_db: float = -60.0, audio_level: int = 0) -> None:
        await self._broadcast({
            "event": "speech.vad",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
            "speaker": self.speaker,
            "direction": self.direction,
            "vadState": vad_state,
            "silenceDurationMs": silence_duration_ms,
            "volumeDb": volume_db,
            "audioLevel": audio_level,
            "startTimeMs": self.turn_start_ms,
            "endTimeMs": self.turn_end_ms,
            "sendTimeMs": int(time.time() * 1000),
        }, publish_to_rabbitmq=False)

    async def _on_heartbeat(self, event: dict) -> None:
        await self._ack(event.get("seq"), True, "ok")

    async def _start_funasr(self) -> None:
        """Compatibility wrapper used by existing tests and maintenance tools."""
        await self._start_provider_segment(FUNASR)

    async def _stop_funasr(self) -> None:
        """Compatibility wrapper for finishing the active segment provider."""
        await self._finish_current_provider()

    async def _on_call_ended(self, event: dict) -> None:
        LOG.info("call.ended callId=%s", self.call_id)
        self.call_ended = True
        _forget_model_preference(self.call_id)
        if self._switch_task is not None and not self._switch_task.done():
            await asyncio.gather(self._switch_task, return_exceptions=True)
        completed_recordings = []
        residual_audio = self._vad.flush()
        if residual_audio:
            completed_recordings.extend(self._turn_recordings.append_vad_segment(self._recording_chunk(
                residual_audio, segment_id=self.current_segment_id
            )))
        completed_recordings.extend(self._turn_recordings.finalize_call(self.call_id))
        for item in completed_recordings:
            await self._publish_or_defer_completed_turn_text(item)
            self._schedule_completed_audio_save(item)
        await self._finish_current_provider(residual_audio)
        await self._ack(event.get("seq"), True, "ok")
        await self._broadcast({
            "event": "call.ended",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
        })
        _call_hold_state.clear_call(self.call_id)

    async def _ack(self, seq, accepted: bool, message: str) -> None:
        try:
            await self.client_ws.send_str(json.dumps({
                "type": "ack",
                "callId": self.call_id,
                "callfrom": self.callfrom,
                "callto": self.callto,
                "receivedSeq": seq or 0,
                "accepted": accepted,
                "message": message,
            }, ensure_ascii=False))
        except ClientConnectionResetError:
            LOG.info("客户端已断连，跳过 ack, callId=%s seq=%s", self.call_id, seq)

    # ── FunASR → 客户端 ────────────────────────────────────────────

    async def _upstream_to_client(self, upstream_ws=None, close_after_offline: bool = False) -> None:
        ws = upstream_ws or self.upstream_ws
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
                if msg.type != WSMsgType.TEXT:
                    continue

                try:
                    result = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                raw_text = result.get("text", "")
                text = _normalize_asr_text(raw_text)
                result["text"] = text
                is_final = result.get("is_final", False)
                mode = result.get("mode", "")

                wav_name = result.pop("wav_name", self.call_id)
                segment_id = self._segment_id_from_wav_name(wav_name)
                result["callId"] = self.call_id
                result["segmentId"] = segment_id
                print(f"[FunASR] callfrom={self.callfrom} callto={self.callto} speaker={self.speaker} {json.dumps(result, ensure_ascii=False)}")

                if not text:
                    continue
                if segment_id and segment_id not in self._valid_segment_ids:
                    LOG.debug(
                        "忽略无 VAD 语音的 FunASR 文本, callId=%s segmentId=%s",
                        self.call_id, segment_id,
                    )
                    continue
                if self._hotword_manager.update_from_recognized_text(text):
                    LOG.info(
                        "hotword stage inferred callId=%s stage=%s text=%s",
                        self.call_id,
                        self._hotword_manager.stage.value,
                        text[:80],
                    )

                # 只用 mode 判断离线结果（xhw FunASR 所有 streaming 也 is_final=true）
                is_offline = mode in ("2pass-offline", "offline")
                await self._broadcast_speech_to_monitors(
                    text,
                    segment_id=segment_id,
                    provider=FUNASR,
                    publish_to_rabbitmq=False,
                )

                if not is_offline:
                    try:
                        await self._send_streaming_text(text, segment_id=segment_id, mode=mode)
                    except ClientConnectionResetError:
                        LOG.info("客户端已断连，跳过 streaming text, callId=%s", self.call_id)
                        break

                if is_offline:
                    if segment_id:
                        self._offline_segments.add(segment_id)
                    try:
                        await self._send_speech_final(
                            text,
                            segment_id=segment_id,
                            provider=FUNASR,
                        )
                    except ClientConnectionResetError:
                        LOG.info("客户端已断连，跳过 speech.final, callId=%s", self.call_id)
                        break
                    LOG.info("speech.final callId=%s text=%s", self.call_id, text[:80])

                    if close_after_offline or self.call_ended:
                        break
        except Exception:
            LOG.exception("FunASR 下行处理异常, callId=%s", self.call_id)
        finally:
            if close_after_offline:
                try:
                    if not getattr(ws, "closed", False):
                        await ws.close()
                except Exception:
                    pass

    async def _send_streaming_text(self, text: str, segment_id: str | None = None, mode: str = "streaming") -> None:
        payload = {
            "mode": mode or "streaming",
            "text": _normalize_asr_text(text),
            "is_final": False,
            "callId": self.call_id,
            "segmentId": segment_id or self.current_segment_id,
        }
        await self.client_ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def _send_call_correction(self, correction: dict) -> None:
        payload = dict(correction)
        payload.setdefault("event", "call.corrected")
        payload.setdefault("eventType", "call.corrected")
        payload.setdefault("callId", self.call_id)
        payload.setdefault("project", self.project)
        if not str(payload.get("correctedText") or "").strip():
            return
        await self.client_ws.send_str(json.dumps(payload, ensure_ascii=False))

    def _segment_id_from_wav_name(self, wav_name: str) -> str | None:
        prefix = f"{self.call_id}__"
        if isinstance(wav_name, str) and wav_name.startswith(prefix):
            return wav_name[len(prefix):] or None
        return self.current_segment_id

    async def _send_speech_final(
        self,
        text: str,
        segment_id: str | None = None,
        provider: str | None = None,
    ) -> None:
        text = _normalize_asr_text(text)
        self.out_seq += 1
        now_ms = int(time.time() * 1000)
        speech = {
            "schemaVersion": "1.0",
            "eventId": f"evt-{self.call_id}-{self.out_seq:06d}",
            "eventType": "speech.final",
            "callId": self.call_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
            "streamId": self.stream_id,
            "seq": self.out_seq,
            "timestampMs": 0,
            "sendTimeMs": now_ms,
            "sourceSystem": "asr-bridge",
            "payload": {
                "segmentId": segment_id or f"seg-{self.out_seq:04d}",
                "speaker": self.speaker,
                "direction": self.direction,
                "startTimeMs": 0,
                "endTimeMs": 0,
                "text": text,
                "confidence": 0.9,
                "language": "zh-CN",
            },
        }
        if provider:
            speech["payload"]["provider"] = provider
        await self.client_ws.send_str(json.dumps(speech, ensure_ascii=False))

    def _global_speech_key(self, event: dict) -> tuple[str, str, str] | None:
        segment_id = str(event.get("segmentId") or "").strip()
        call_id = str(event.get("callId") or self.call_id or "").strip()
        if not segment_id or not call_id:
            return None
        key = pair_key(
            str(event.get("callfrom") or self.callfrom),
            str(event.get("callto") or self.callto),
            call_id,
        )
        return key, call_id, segment_id

    def _completed_speech_key(self, completed: CompletedTurnRecording, segment_id: str) -> tuple[str, str, str]:
        return pair_key(completed.callfrom, completed.callto, completed.call_id), completed.call_id, segment_id

    def _completed_pending_key(self, completed: CompletedTurnRecording) -> tuple[str, str, tuple[str, ...]]:
        return (
            pair_key(completed.callfrom, completed.callto, completed.call_id),
            completed.call_id,
            tuple(sid for sid in completed.segment_ids if sid),
        )

    def _remember_transcript_segment(self, event: dict) -> None:
        text = _normalize_asr_text(event.get("text") or "")
        if text:
            event["text"] = text
        if not text:
            return
        segment_id = str(event.get("segmentId") or f"segment-{len(self._transcript_segments) + 1:04d}")
        self._transcript_segments[segment_id] = TranscriptTurn(
            segment_id=segment_id,
            speaker=str(event.get("speaker") or self.speaker),
            direction=str(event.get("direction") or self.direction),
            text=text,
            start_time_ms=int(event.get("startTimeMs") or 0),
            end_time_ms=int(event.get("endTimeMs") or 0),
        )

    def _transcript_turns(self) -> list[TranscriptTurn]:
        return sorted(
            self._transcript_segments.values(),
            key=lambda turn: (turn.start_time_ms, turn.end_time_ms, turn.segment_id),
        )

    def _clean_turn_text_part(self, raw: str) -> str:
        return _normalize_asr_text(raw)

    def _merge_turn_text_parts(self, parts: list[str]) -> str:
        merged = ""
        for raw in parts:
            text = self._clean_turn_text_part(raw)
            if not text:
                continue
            if not merged:
                merged = text
                continue
            if text == merged or text in merged:
                continue
            if text.startswith(merged):
                merged = text
                continue
            if merged.endswith(text):
                continue

            # VAD 重新开段后，ASR 可能从上一段中间重新识别，例如：
            # “...我这边困了三十多个人好像是叫深圳软件园一七” +
            # “我这边困了三十多个人好像是叫深圳软件园一七七栋”。
            # 找到“新段前缀”在已合并文本里的最长位置，用新段续写，避免整句重复。
            replaced = False
            max_prefix = min(len(text), len(merged))
            for prefix_len in range(max_prefix, 7, -1):
                prefix = text[:prefix_len]
                pos = merged.find(prefix)
                if pos >= 0:
                    merged = merged[:pos] + text
                    replaced = True
                    break
            if replaced:
                continue

            # 普通首尾重叠，例如 A=“深圳软件园一七”，B=“一七七栋”。
            max_overlap = min(len(merged), len(text))
            for overlap in range(max_overlap, 3, -1):
                if merged.endswith(text[:overlap]):
                    merged += text[overlap:]
                    replaced = True
                    break
            if replaced:
                continue

            merged += text
        return merged.strip()

    def _schedule_call_correction(self) -> None:
        if self._correction_task and not self._correction_task.done():
            return
        self._correction_task = asyncio.create_task(self._publish_call_correction())

    def _turn_correction_key(self, event: dict) -> tuple[str, ...]:
        segment_ids = event.get("segmentIds")
        if isinstance(segment_ids, list):
            ids = tuple(str(sid) for sid in segment_ids if sid)
        else:
            ids = tuple()
        if not ids:
            segment_id = str(event.get("segmentId") or "").strip()
            ids = (segment_id,) if segment_id else (str(event.get("sendTimeMs") or ""),)
        return ids

    def _transcript_turn_from_event(self, event: dict) -> TranscriptTurn:
        return TranscriptTurn(
            segment_id=str(event.get("segmentId") or ""),
            speaker=str(event.get("speaker") or self.speaker),
            direction=str(event.get("direction") or self.direction),
            text=_normalize_asr_text(event.get("text") or ""),
            start_time_ms=int(event.get("startTimeMs") or 0),
            end_time_ms=int(event.get("endTimeMs") or 0),
        )

    def _schedule_turn_correction(self, event: dict) -> None:
        if hasattr(self._call_postprocessor, "enabled") and not self._call_postprocessor.enabled:
            return
        if not (event.get("text") or "").strip():
            return
        key = self._turn_correction_key(event)
        if key in self._corrected_turn_keys:
            return
        self._corrected_turn_keys.add(key)
        self._schedule_background_task(
            self._publish_turn_correction(dict(event)),
            "publish_turn_correction",
        )

    async def _publish_turn_correction(self, event: dict) -> None:
        turn = self._transcript_turn_from_event(event)
        if not turn.text.strip():
            return
        try:
            async with _ai_correction_semaphore:
                correction = await asyncio.to_thread(
                    self._call_postprocessor.build_turn_event,
                    call_id=str(event.get("callId") or self.call_id),
                    callfrom=str(event.get("callfrom") or self.callfrom),
                    callto=str(event.get("callto") or self.callto),
                    turn=turn,
                )
        except Exception:
            LOG.exception(
                "AI turn 纠正失败，已忽略, callId=%s segmentId=%s",
                self.call_id,
                turn.segment_id,
            )
            return
        if not correction:
            return
        if isinstance(event.get("segmentIds"), list):
            correction["segmentIds"] = list(event["segmentIds"])
        correction["finalSource"] = event.get("finalSource", "")
        correction["sendTimeMs"] = int(time.time() * 1000)
        LOG.info(
            "call.corrected turn-complete callId=%s segmentId=%s elapsed=%s text=%s",
            correction.get("callId"),
            correction.get("segmentId"),
            correction.get("llmElapsedMs"),
            str(correction.get("correctedText") or "")[:80],
        )
        await self._broadcast(correction)
        try:
            await self._send_call_correction(correction)
        except ClientConnectionResetError:
            LOG.info("客户端已断连，跳过 call.corrected, callId=%s", self.call_id)

    async def _publish_call_correction(self) -> None:
        turns = self._transcript_turns()
        if not turns:
            return
        try:
            event = await asyncio.to_thread(
                self._call_postprocessor.build_event,
                call_id=self.call_id,
                callfrom=self.callfrom,
                callto=self.callto,
                turns=turns,
            )
        except Exception:
            LOG.exception("AI 纠正失败，已忽略, callId=%s", self.call_id)
            return
        if event:
            await self._broadcast(event)

    async def _broadcast_speech_to_monitors(
        self,
        text: str,
        segment_id: str | None = None,
        provider: str | None = None,
        *,
        publish_to_rabbitmq: bool = False,
    ) -> None:
        if self._is_hold_active():
            LOG.info("call held, skip speech.final callId=%s speaker=%s", self.call_id, self.speaker)
            return

        text = _normalize_asr_text(text)
        # 计算当前 turn 时长 (ms)
        turn_duration_ms = max(0, self.turn_end_ms - self.turn_start_ms)
        event = {
            "event": "speech.final",
            "callId": self.call_id,
            "segmentId": segment_id or self.current_segment_id,
            "callfrom": self.callfrom,
            "callto": self.callto,
            "speaker": self.speaker,
            "direction": self.direction,
            "text": text,
            "startTimeMs": self.turn_start_ms,
            "endTimeMs": self.turn_end_ms,
            "durationMs": turn_duration_ms,
            "sendTimeMs": int(time.time() * 1000),
        }
        if provider:
            event["provider"] = provider
        stable_segment_id = str(event.get("segmentId") or "")
        if stable_segment_id:
            event_copy = dict(event)
            self._latest_speech_events[stable_segment_id] = event_copy
            global_key = self._global_speech_key(event_copy)
            if global_key:
                _global_speech_events[global_key] = event_copy
        self._remember_transcript_segment(event)
        await self._retry_pending_completed_turn_texts()
        await self._broadcast(event, publish_to_rabbitmq=publish_to_rabbitmq)
        self._schedule_remember_speech(event)
        completed_recordings = self._turn_recordings.mark_effective_speech(
            call_id=self.call_id,
            callfrom=self.callfrom,
            callto=self.callto,
            speaker=self.speaker,
        )
        for item in completed_recordings:
            await self._publish_or_defer_completed_turn_text(item)
            self._schedule_completed_audio_save(item)

    async def _publish_or_defer_completed_turn_text(self, completed: CompletedTurnRecording) -> None:
        published = await self._publish_completed_turn_text(completed)
        if published:
            return
        key = self._completed_pending_key(completed)
        if key[2]:
            _global_pending_completed_texts[key] = completed

    async def _retry_pending_completed_turn_texts(self) -> None:
        if not _global_pending_completed_texts:
            return
        published_keys = []
        for key, item in list(_global_pending_completed_texts.items()):
            if await self._publish_completed_turn_text(item):
                published_keys.append(key)
        for key in published_keys:
            _global_pending_completed_texts.pop(key, None)

    async def _publish_completed_turn_text(self, completed: CompletedTurnRecording) -> bool:
        if self._is_hold_active(call_id=completed.call_id, callfrom=completed.callfrom, callto=completed.callto):
            LOG.info("call held, skip turn-complete speech.final callId=%s speaker=%s", completed.call_id, completed.speaker)
            return True

        segment_ids = [sid for sid in completed.segment_ids if sid]
        if not segment_ids:
            return True
        if all(sid in self._rabbitmq_published_segments for sid in segment_ids):
            return True

        parts = []
        providers = []
        providers_complete = True
        last_event = None
        for sid in segment_ids:
            event = self._latest_speech_events.get(sid) or _global_speech_events.get(self._completed_speech_key(completed, sid))
            if not event:
                continue
            text = str(event.get("text") or "").strip()
            if text:
                parts.append(text)
                last_event = event
                event_providers = event.get("providers")
                if isinstance(event_providers, list) and event_providers:
                    source_providers = [
                        str(item).strip()
                        for item in event_providers
                        if str(item).strip()
                    ]
                else:
                    provider = str(event.get("provider") or "").strip()
                    source_providers = [provider] if provider and provider != "mixed" else []
                if not source_providers:
                    providers_complete = False
                for provider in source_providers:
                    if provider not in providers:
                        providers.append(provider)
        if not parts or not last_event:
            LOG.info(
                "speech.final turn-complete deferred callId=%s segmentIds=%s reason=missing_text",
                completed.call_id,
                ",".join(segment_ids),
            )
            return False

        event = dict(last_event)
        event["segmentId"] = segment_ids[0]
        event["segmentIds"] = segment_ids
        if providers_complete and providers:
            event["providers"] = providers
            event["provider"] = providers[0] if len(providers) == 1 else "mixed"
        else:
            event.pop("provider", None)
            event.pop("providers", None)
        # turn 完成后推送给 RabbitMQ 的稳定文本应代表这一方本轮完整说话，
        # 而不是最后一个 VAD 小段。合并时跳过完全重复和 progressive 前缀更新。
        event["text"] = self._merge_turn_text_parts(parts)
        event["startTimeMs"] = completed.start_time_ms
        event["endTimeMs"] = completed.end_time_ms
        event["durationMs"] = max(0, completed.end_time_ms - completed.start_time_ms)
        event["speaker"] = completed.speaker
        event["direction"] = completed.direction
        event["finalSource"] = "offline" if any(sid in self._offline_segments for sid in segment_ids) else "turn-complete-streaming-fallback"
        event["sendTimeMs"] = int(time.time() * 1000)

        self._rabbitmq_published_segments.update(segment_ids)
        LOG.info(
            "speech.final turn-complete callId=%s segmentIds=%s source=%s text=%s",
            completed.call_id,
            ",".join(segment_ids),
            event["finalSource"],
            event["text"][:80],
        )
        await self._broadcast(event, publish_to_rabbitmq=True)
        self._schedule_turn_correction(event)
        return True

    def _is_hold_active(self, *, call_id: str | None = None, callfrom: str | None = None, callto: str | None = None) -> bool:
        return _call_hold_state.is_held(
            call_id=self.call_id if call_id is None else call_id,
            callfrom=self.callfrom if callfrom is None else callfrom,
            callto=self.callto if callto is None else callto,
        )
