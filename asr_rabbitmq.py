"""RabbitMQ publisher for ASR realtime events."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pika

LOG = logging.getLogger("asr-rabbitmq")
_ROUTING_UNSAFE_RE = re.compile(r"[^0-9A-Za-z_-]+")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _safe_routing_part(value: Any) -> str:
    text = str(value or "").strip()
    text = _ROUTING_UNSAFE_RE.sub("_", text).strip("_")
    return text or "unknown"


@dataclass
class AsrRabbitMQPublisher:
    """Publish ASR events to a direct exchange, partitioned by callTo."""

    enabled: bool = False
    host: str = ""
    port: int = 5672
    vhost: str = "ai"
    user: str = "guest"
    password: str = "guest"
    exchange: str = "ids:asr"
    source: str = "ids:asr"
    routing_prefix: str = "asr"
    fixed_routing_key: bool = False

    def routing_key(self, event: dict) -> str:
        if self.fixed_routing_key:
            return self.routing_prefix
        return f"{self.routing_prefix}.{_safe_routing_part(event.get('callto'))}"

    def build_cloud_event(self, event: dict) -> dict:
        event_name = str(event.get("event") or "unknown")
        return {
            "id": str(event.get("eventId") or uuid.uuid4().hex),
            "source": self.source,
            "type": f"{self.source}:{event_name}",
            "specversion": "1.0",
            "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "data": dict(event),
        }

    def message(self, event: dict) -> dict:
        return {
            "exchange": self.exchange,
            "routingKey": self.routing_key(event),
            "payload": self.build_cloud_event(event),
        }

    def publish(self, event: dict) -> dict | None:
        if not self.enabled or not self.host:
            return None
        message = self.message(event)
        threading.Thread(
            target=self._publish_sync,
            args=(message["routingKey"], message["payload"]),
            daemon=True,
        ).start()
        return message

    def _publish_sync(self, routing_key: str, payload: dict) -> None:
        try:
            credentials = pika.PlainCredentials(self.user, self.password)
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=self.host,
                    port=self.port,
                    virtual_host=self.vhost,
                    credentials=credentials,
                )
            )
            channel = connection.channel()
            channel.exchange_declare(
                exchange=self.exchange,
                exchange_type="direct",
                durable=True,
            )
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=json.dumps(payload, ensure_ascii=False),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
            )
            connection.close()
        except Exception:
            LOG.exception("ASR RabbitMQ publish failed, rk=%s", routing_key)


def create_asr_rabbitmq_publisher(
    env_prefix: str = "ASR_RABBITMQ_",
    default_exchange: str = "ids:asr",
    default_source: str = "ids:asr",
    default_routing_prefix: str = "asr",
) -> AsrRabbitMQPublisher | None:
    """创建 RabbitMQ 发布器。

    主通道 (env_prefix="ASR_RABBITMQ_")：始终创建，由 ASR_RABBITMQ_ENABLED 控制启停。
    副通道 (env_prefix="ASR_RABBITMQ_QS_")：仅在显式配置时创建，复用主通道连接信息。
    """
    is_primary = (env_prefix == "ASR_RABBITMQ_")

    if is_primary:
        host = os.getenv("ASR_RABBITMQ_HOST", "").strip()
        return AsrRabbitMQPublisher(
            enabled=_env_bool("ASR_RABBITMQ_ENABLED"),
            host=host,
            port=int(os.getenv("ASR_RABBITMQ_PORT", "5672")),
            vhost=os.getenv("ASR_RABBITMQ_VHOST", "ai").strip() or "ai",
            user=os.getenv("ASR_RABBITMQ_USER", "guest").strip() or "guest",
            password=os.getenv("ASR_RABBITMQ_PASS", "guest"),
            exchange=os.getenv("ASR_RABBITMQ_EXCHANGE", "ids:asr").strip() or "ids:asr",
            source=os.getenv("ASR_RABBITMQ_SOURCE", "ids:asr").strip() or "ids:asr",
            routing_prefix=os.getenv("ASR_RABBITMQ_ROUTING_PREFIX", "asr").strip() or "asr",
        )

    # 副通道：必须显式启用或配置 HOST
    if not _env_bool(f"{env_prefix}ENABLED", default=False):
        return None
    host = os.getenv(f"{env_prefix}HOST", "").strip() or os.getenv("ASR_RABBITMQ_HOST", "").strip()
    if not host:
        return None

    return AsrRabbitMQPublisher(
        enabled=True,
        host=host,
        port=int(os.getenv(f"{env_prefix}PORT", os.getenv("ASR_RABBITMQ_PORT", "5672"))),
        vhost=os.getenv(f"{env_prefix}VHOST", os.getenv("ASR_RABBITMQ_VHOST", "ai")).strip() or "ai",
        user=os.getenv(f"{env_prefix}USER", os.getenv("ASR_RABBITMQ_USER", "guest")).strip() or "guest",
        password=os.getenv(f"{env_prefix}PASS", os.getenv("ASR_RABBITMQ_PASS", "guest")),
        exchange=os.getenv(f"{env_prefix}EXCHANGE", default_exchange).strip() or default_exchange,
        source=os.getenv(f"{env_prefix}SOURCE", default_source).strip() or default_source,
        routing_prefix=os.getenv(f"{env_prefix}ROUTING_PREFIX", default_routing_prefix).strip() or default_routing_prefix,
        fixed_routing_key=_env_bool(f"{env_prefix}FIXED_ROUTING_KEY", default=False),
    )
