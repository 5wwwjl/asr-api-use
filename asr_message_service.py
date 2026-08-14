"""Asynchronous message-service publisher for stable ASR events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp

LOG = logging.getLogger("asr-message-service")
ASR_MESSAGE_CONTENT = "ASR实时语音转写"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MessageServiceConfig:
    enabled: bool = False
    uac_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    send_url: str = ""
    tenant_id: str = ""
    target_client: str = "ids-seat-web"
    target_desc: str = "坐席端"
    queue_size: int = 1000
    connect_timeout_seconds: float = 3.0
    total_timeout_seconds: float = 10.0
    token_expiry_skew_seconds: int = 60
    retry_delay_seconds: float = 0.5
    shutdown_drain_seconds: float = 3.0
    fallback_rabbitmq: bool = False
    gateway_ip: str = ""
    host_header: str = ""

    @classmethod
    def from_env(cls) -> "MessageServiceConfig":
        return cls(
            enabled=_env_bool("ASR_MESSAGE_ENABLED", default=False),
            uac_base_url=os.getenv("UAC_BASE_URL", "").strip(),
            client_id=os.getenv("UAC_CLIENT_ID", "").strip(),
            client_secret=os.getenv("UAC_CLIENT_SECRET", ""),
            send_url=os.getenv("MESSAGE_SEND_URL", "").strip(),
            tenant_id=os.getenv("MESSAGE_TENANT_ID", "").strip(),
            target_client=os.getenv("ASR_MESSAGE_TARGET_CLIENT", "ids-seat-web").strip()
            or "ids-seat-web",
            target_desc=os.getenv("ASR_MESSAGE_TARGET_DESC", "坐席端").strip() or "坐席端",
            queue_size=max(1, _env_int("ASR_MESSAGE_QUEUE_SIZE", 1000)),
            connect_timeout_seconds=max(
                0.1, _env_float("ASR_MESSAGE_CONNECT_TIMEOUT_SECONDS", 3.0)
            ),
            total_timeout_seconds=max(
                0.1, _env_float("ASR_MESSAGE_TOTAL_TIMEOUT_SECONDS", 10.0)
            ),
            token_expiry_skew_seconds=max(
                0, _env_int("ASR_MESSAGE_TOKEN_EXPIRY_SKEW_SECONDS", 60)
            ),
            retry_delay_seconds=max(
                0.0, _env_float("ASR_MESSAGE_RETRY_DELAY_SECONDS", 0.5)
            ),
            shutdown_drain_seconds=max(
                0.0, _env_float("ASR_MESSAGE_SHUTDOWN_DRAIN_SECONDS", 3.0)
            ),
            fallback_rabbitmq=_env_bool(
                "ASR_MESSAGE_FALLBACK_RABBITMQ", default=False
            ),
            gateway_ip=os.getenv("ASR_MESSAGE_GATEWAY_IP", "").strip(),
            host_header=os.getenv("ASR_MESSAGE_HOST_HEADER", "").strip(),
        )

    def missing_required(self) -> list[str]:
        required = {
            "UAC_BASE_URL": self.uac_base_url,
            "UAC_CLIENT_ID": self.client_id,
            "UAC_CLIENT_SECRET": self.client_secret,
            "MESSAGE_SEND_URL": self.send_url,
            "MESSAGE_TENANT_ID": self.tenant_id,
        }
        return [name for name, value in required.items() if not value]

    def request_url(self, url: str) -> str:
        if not self.gateway_ip:
            return url
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit(
            (parsed.scheme, f"{self.gateway_ip}{port}", parsed.path, parsed.query, parsed.fragment)
        )

    def gateway_headers(self, url: str) -> dict[str, str]:
        if not self.gateway_ip:
            return {}
        parsed = urlsplit(url)
        host = self.host_header or parsed.hostname or ""
        if parsed.port and parsed.port not in {80, 443}:
            host = f"{host}:{parsed.port}"
        return {"Host": host} if host else {}


class AsrMessageServicePublisher:
    """Queue stable ASR events and deliver them through the message service."""

    def __init__(
        self,
        config: MessageServiceConfig,
        *,
        fallback_publisher: Any = None,
        session_factory: Callable[..., aiohttp.ClientSession] | None = None,
    ) -> None:
        self.config = config
        self.fallback_publisher = fallback_publisher
        self._session_factory = session_factory or aiohttp.ClientSession
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=config.queue_size
        )
        self._session: aiohttp.ClientSession | None = None
        self._worker_task: asyncio.Task | None = None
        self._token_lock = asyncio.Lock()
        self._access_token = ""
        self._token_expires_at = 0.0
        self._started = False
        self._closing = False
        self.dropped_count = 0
        self.sent_count = 0
        self.failed_count = 0

    @property
    def started(self) -> bool:
        return self._started

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> bool:
        if self._started:
            return True
        if not self.config.enabled:
            LOG.info("ASR message-service publisher disabled")
            return False
        missing = self.config.missing_required()
        if missing:
            LOG.error(
                "ASR message-service publisher disabled: missing config keys=%s",
                ",".join(missing),
            )
            return False

        timeout = aiohttp.ClientTimeout(
            total=self.config.total_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
        )
        self._session = self._session_factory(timeout=timeout, trust_env=False)
        self._closing = False
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="asr-message-service-publisher"
        )
        self._started = True
        LOG.info(
            "ASR message-service publisher started target=%s queueSize=%s fallbackRabbitMQ=%s",
            self.config.target_client,
            self.config.queue_size,
            self.config.fallback_rabbitmq,
        )
        return True

    def enqueue(self, event: dict[str, Any]) -> bool:
        if not str(event.get("callto") or "").strip():
            LOG.warning(
                "ASR message rejected: empty callto event=%s callId=%s",
                event.get("event", ""),
                event.get("callId", ""),
            )
            return False
        if not self._started or self._closing:
            return False
        item = dict(event)
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_count += 1
                LOG.warning(
                    "ASR message queue full; dropped oldest eventId=%s event=%s callId=%s callto=%s",
                    dropped.get("eventId", ""),
                    dropped.get("event", ""),
                    dropped.get("callId", ""),
                    dropped.get("callto", ""),
                )
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(item)
        return True

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        if self.config.shutdown_drain_seconds > 0:
            try:
                await asyncio.wait_for(
                    self._queue.join(), timeout=self.config.shutdown_drain_seconds
                )
            except asyncio.TimeoutError:
                LOG.warning(
                    "ASR message queue drain timed out pending=%s", self._queue.qsize()
                )
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._access_token = ""
        self._token_expires_at = 0.0
        self._started = False
        LOG.info(
            "ASR message-service publisher stopped sent=%s failed=%s dropped=%s pending=%s",
            self.sent_count,
            self.failed_count,
            self.dropped_count,
            self._queue.qsize(),
        )

    async def _worker_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._deliver(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed_count += 1
                LOG.exception(
                    "Unexpected ASR message delivery error event=%s callId=%s callto=%s",
                    event.get("event", ""),
                    event.get("callId", ""),
                    event.get("callto", ""),
                )
                self._fallback(event)
            finally:
                self._queue.task_done()

    async def _deliver(self, event: dict[str, Any]) -> bool:
        cloud_event = self.build_cloud_event(event)
        body = self.build_request_body(event, cloud_event=cloud_event)
        last_status: int | None = None
        last_code: Any = None
        last_message = ""

        for attempt in range(2):
            try:
                token = await self._get_token(force=False)
                status, payload, text = await self._post_message(token, body)
                last_status = status
                last_code = payload.get("code") if isinstance(payload, dict) else None
                last_message = (
                    str(payload.get("message") or "")
                    if isinstance(payload, dict)
                    else text[:500]
                )
                if self._is_success(status, payload):
                    self.sent_count += 1
                    LOG.info(
                        "ASR message sent eventId=%s event=%s callId=%s callto=%s",
                        cloud_event["id"],
                        event.get("event", ""),
                        event.get("callId", ""),
                        event.get("callto", ""),
                    )
                    return True
                if attempt == 0 and self._is_auth_error(status, payload):
                    self._invalidate_token()
                    continue
                if attempt == 0 and status >= 500:
                    await asyncio.sleep(self.config.retry_delay_seconds)
                    continue
                break
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_message = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    await asyncio.sleep(self.config.retry_delay_seconds)
                    continue
                break

        self.failed_count += 1
        LOG.error(
            "ASR message send failed eventId=%s event=%s callId=%s callto=%s http=%s code=%s response=%s",
            cloud_event["id"],
            event.get("event", ""),
            event.get("callId", ""),
            event.get("callto", ""),
            last_status,
            last_code,
            last_message[:500],
        )
        self._fallback(event)
        return False

    async def _get_token(self, *, force: bool) -> str:
        now = time.monotonic()
        if not force and self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._token_expires_at:
                return self._access_token
            if self._session is None:
                raise RuntimeError("message-service HTTP session is not started")

            login_url = f"{self.config.uac_base_url.rstrip('/')}/loginByClientCredentials"
            headers = self.config.gateway_headers(login_url)
            async with self._session.post(
                self.config.request_url(login_url),
                headers=headers,
                json={
                    "clientId": self.config.client_id,
                    "clientSecret": self.config.client_secret,
                },
            ) as response:
                text = await response.text()
                payload = self._json_or_empty(text)
                data = payload.get("data") if isinstance(payload, dict) else None
                token = data.get("access_token") if isinstance(data, dict) else None
                if response.status != 200 or payload.get("code") != 200 or not token:
                    message = payload.get("message") if isinstance(payload, dict) else text[:500]
                    raise RuntimeError(
                        f"UAC login failed http={response.status} code={payload.get('code')} message={message}"
                    )
                expires_in = int(data.get("expires_in") or 0)
                usable_seconds = max(
                    1, expires_in - self.config.token_expiry_skew_seconds
                )
                self._access_token = str(token)
                self._token_expires_at = time.monotonic() + usable_seconds
                LOG.info(
                    "UAC client token acquired clientId=%s expiresIn=%s",
                    self.config.client_id,
                    expires_in,
                )
                return self._access_token

    async def _post_message(
        self, token: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any], str]:
        if self._session is None:
            raise RuntimeError("message-service HTTP session is not started")
        headers = self.config.gateway_headers(self.config.send_url)
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "clientId": self.config.client_id,
                "tenantId": self.config.tenant_id,
            }
        )
        async with self._session.post(
            self.config.request_url(self.config.send_url), headers=headers, json=body
        ) as response:
            text = await response.text()
            return response.status, self._json_or_empty(text), text

    def _invalidate_token(self) -> None:
        self._access_token = ""
        self._token_expires_at = 0.0

    @staticmethod
    def _json_or_empty(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_success(status: int, payload: dict[str, Any]) -> bool:
        if not 200 <= status < 300:
            return False
        code = payload.get("code")
        success = payload.get("success")
        return code in {None, 200} and success is not False

    @staticmethod
    def _is_auth_error(status: int, payload: dict[str, Any]) -> bool:
        return status in {401, 412} or payload.get("code") == 41202

    def _fallback(self, event: dict[str, Any]) -> None:
        if not self.config.fallback_rabbitmq or self.fallback_publisher is None:
            return
        try:
            self.fallback_publisher.publish(event)
            LOG.warning(
                "ASR message fell back to RabbitMQ event=%s callId=%s callto=%s",
                event.get("event", ""),
                event.get("callId", ""),
                event.get("callto", ""),
            )
        except Exception:
            LOG.exception(
                "ASR message RabbitMQ fallback failed event=%s callId=%s callto=%s",
                event.get("event", ""),
                event.get("callId", ""),
                event.get("callto", ""),
            )

    @staticmethod
    def build_cloud_event(event: dict[str, Any]) -> dict[str, Any]:
        event_name = str(event.get("event") or "unknown")
        return {
            "id": str(event.get("eventId") or uuid.uuid4().hex),
            "source": "ids:asr",
            "type": f"ids:asr:{event_name}",
            "specversion": "1.0",
            "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "data": dict(event),
        }

    def build_request_body(
        self,
        event: dict[str, Any],
        *,
        cloud_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_name = str(event.get("event") or "unknown")
        callto = str(event.get("callto") or "").strip()
        if not callto:
            raise ValueError("ASR message requires non-empty callto")
        cloud_event = cloud_event or self.build_cloud_event(event)
        return {
            "userMsgBodyList": [
                {
                    "content": ASR_MESSAGE_CONTENT,
                    "topics": [{"type": "SEAT", "key": callto}],
                    "clientDto": {
                        "code": self.config.target_client,
                        "desc": self.config.target_desc,
                    },
                    "notifyTypeDto": {
                        "notifyType": "asr",
                        "notifySubType": event_name,
                    },
                    "request": {
                        "channel": "WEBSOCKET",
                        "customContent": cloud_event,
                    },
                }
            ]
        }


def create_asr_message_service_publisher(
    *, fallback_publisher: Any = None
) -> AsrMessageServicePublisher:
    return AsrMessageServicePublisher(
        MessageServiceConfig.from_env(), fallback_publisher=fallback_publisher
    )
