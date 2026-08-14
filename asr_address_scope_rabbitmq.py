"""Long-lived RabbitMQ consumer for location address-scope events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Awaitable, Callable

import pika

from asr_address_scope import InvalidAddressScopeEvent


LOG = logging.getLogger("asr-address-scope-rabbitmq")
EventHandler = Callable[[dict[str, object]], Awaitable[object]]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AddressScopeRabbitMQConfig:
    enabled: bool = False
    host: str = ""
    port: int = 5672
    vhost: str = "/location"
    user: str = ""
    password: str = ""
    queue: str = "location.address-scope.hotword.v1"
    handler_timeout_seconds: float = 15.0
    reconnect_max_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "AddressScopeRabbitMQConfig":
        return cls(
            enabled=_env_bool("ASR_ADDRESS_SCOPE_MQ_ENABLED"),
            host=os.getenv("ASR_ADDRESS_SCOPE_MQ_HOST", "").strip(),
            port=int(os.getenv("ASR_ADDRESS_SCOPE_MQ_PORT", "5672")),
            vhost=os.getenv("ASR_ADDRESS_SCOPE_MQ_VHOST", "/location").strip() or "/location",
            user=os.getenv("ASR_ADDRESS_SCOPE_MQ_USER", "").strip(),
            password=os.getenv("ASR_ADDRESS_SCOPE_MQ_PASSWORD", ""),
            queue=os.getenv(
                "ASR_ADDRESS_SCOPE_MQ_QUEUE",
                "location.address-scope.hotword.v1",
            ).strip() or "location.address-scope.hotword.v1",
            handler_timeout_seconds=float(
                os.getenv("ASR_ADDRESS_SCOPE_MQ_HANDLER_TIMEOUT_SECONDS", "15")
            ),
            reconnect_max_seconds=float(
                os.getenv("ASR_ADDRESS_SCOPE_MQ_RECONNECT_MAX_SECONDS", "30")
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host or not self.user or not self.password or not self.queue:
            raise ValueError("address-scope RabbitMQ requires host, user, password and queue")
        if self.port <= 0 or self.handler_timeout_seconds <= 0 or self.reconnect_max_seconds <= 0:
            raise ValueError("address-scope RabbitMQ numeric settings must be positive")


class AddressScopeRabbitMQConsumer:
    """Consumes one message at a time and delegates state changes to asyncio."""

    def __init__(
        self,
        config: AddressScopeRabbitMQConfig,
        *,
        loop: asyncio.AbstractEventLoop,
        handler: EventHandler,
    ) -> None:
        config.validate()
        self._config = config
        self._loop = loop
        self._handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection = None
        self._lock = threading.Lock()
        self.received = 0
        self.acked = 0
        self.requeued = 0
        self.rejected = 0
        self.reconnects = 0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="address-scope-rabbitmq-consumer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.add_callback_threadsafe(connection.close)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout_seconds)
            self._thread = None

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self._thread is not None and self._thread.is_alive(),
            "received": self.received,
            "acked": self.acked,
            "requeued": self.requeued,
            "rejected": self.rejected,
            "reconnects": self.reconnects,
            "lastError": self.last_error,
        }

    def _run(self) -> None:
        retry_delay = 1.0
        while not self._stop.is_set():
            try:
                self._consume_until_stopped()
                retry_delay = 1.0
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.reconnects += 1
                self.last_error = type(exc).__name__
                LOG.warning(
                    "address-scope MQ consumer disconnected error=%s retrySeconds=%.1f",
                    type(exc).__name__,
                    retry_delay,
                )
                self._stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, self._config.reconnect_max_seconds)
            finally:
                with self._lock:
                    self._connection = None

    def _consume_until_stopped(self) -> None:
        credentials = pika.PlainCredentials(self._config.user, self._config.password)
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=self._config.host,
                port=self._config.port,
                virtual_host=self._config.vhost,
                credentials=credentials,
                heartbeat=30,
                blocked_connection_timeout=30,
            )
        )
        with self._lock:
            self._connection = connection
        channel = connection.channel()
        channel.queue_declare(queue=self._config.queue, passive=True)
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=self._config.queue, on_message_callback=self._on_message, auto_ack=False)
        LOG.info("address-scope MQ consumer connected queue=%s vhost=%s", self._config.queue, self._config.vhost)
        while not self._stop.is_set() and connection.is_open:
            connection.process_data_events(time_limit=1)
        if connection.is_open:
            connection.close()

    def _on_message(self, channel, method, _properties, body: bytes) -> None:
        self.received += 1
        try:
            decoded = json.loads(body.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise InvalidAddressScopeEvent("event must be an object")
            future = asyncio.run_coroutine_threadsafe(self._handler(decoded), self._loop)
            future.result(timeout=self._config.handler_timeout_seconds)
        except (UnicodeDecodeError, json.JSONDecodeError, InvalidAddressScopeEvent, ValueError) as exc:
            self.rejected += 1
            LOG.warning("address-scope MQ message rejected error=%s", type(exc).__name__)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except FutureTimeout:
            self.requeued += 1
            LOG.warning("address-scope MQ handler timed out")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        except Exception as exc:
            self.requeued += 1
            LOG.warning("address-scope MQ handler failed error=%s", type(exc).__name__)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        else:
            self.acked += 1
            channel.basic_ack(delivery_tag=method.delivery_tag)


def create_address_scope_rabbitmq_consumer(
    *, loop: asyncio.AbstractEventLoop, handler: EventHandler
) -> AddressScopeRabbitMQConsumer:
    return AddressScopeRabbitMQConsumer(
        AddressScopeRabbitMQConfig.from_env(), loop=loop, handler=handler
    )
