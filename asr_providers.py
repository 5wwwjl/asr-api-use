"""Per-VAD ASR providers used by the bridge session."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping

from aiohttp import WSMsgType

from xfyun_dialect_iat_smoke import (
    DialectResultAccumulator,
    DialectServiceError,
    build_audio_message,
    build_auth_url,
    frame_size_for_rate,
)


FUNASR = "funasr"
XFYUN = "xfyun"
VALID_PROVIDERS = {FUNASR, XFYUN}
_FINAL_FUNASR_MODES = {"2pass-offline", "offline"}
_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_QUEUE_END = object()
LOG = logging.getLogger("asr_providers")


class ProviderError(RuntimeError):
    """A stable, redacted provider failure safe for logs and monitor events."""

    def __init__(self, code: str, message: str, *, provider: str) -> None:
        self.code = str(code)
        self.message = str(message)
        self.provider = str(provider)
        super().__init__(self.message)


class ProviderUnavailableError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    segment_id: str
    text: str = ""
    is_final: bool = False
    mode: str = "streaming"
    error_code: str = ""
    error_message: str = ""
    sid: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error_code)


@dataclass(frozen=True)
class XfyunDialectConfig:
    enabled: bool = False
    app_id: str = ""
    api_key: str = ""
    api_secret: str = ""
    sample_rate: int = 16000
    connect_timeout_seconds: float = 15.0
    result_timeout_seconds: float = 30.0
    max_segment_seconds: float = 55.0
    queue_size: int = 256

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> "XfyunDialectConfig":
        source = values or os.environ

        def _value(name: str, default: str = "") -> str:
            return str(source.get(name, default) or default).strip()

        def _float(name: str, default: float) -> float:
            try:
                return float(_value(name, str(default)))
            except ValueError:
                return default

        def _int(name: str, default: int) -> int:
            try:
                return int(_value(name, str(default)))
            except ValueError:
                return default

        enabled = _value("ASR_XFYUN_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        return cls(
            enabled=enabled,
            app_id=_value("XFYUN_APP_ID"),
            api_key=_value("XFYUN_API_KEY"),
            api_secret=_value("XFYUN_API_SECRET"),
            sample_rate=_int("ASR_XFYUN_SAMPLE_RATE", 16000),
            connect_timeout_seconds=_float("ASR_XFYUN_CONNECT_TIMEOUT_SECONDS", 15.0),
            result_timeout_seconds=_float("ASR_XFYUN_RESULT_TIMEOUT_SECONDS", 30.0),
            max_segment_seconds=_float("ASR_XFYUN_MAX_SEGMENT_SECONDS", 55.0),
            queue_size=max(8, _int("ASR_XFYUN_AUDIO_QUEUE_SIZE", 256)),
        )

    def availability(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "XFYUN_DISABLED"
        if not self.app_id or not self.api_key or not self.api_secret:
            return False, "XFYUN_CREDENTIALS_MISSING"
        if self.sample_rate not in {8000, 16000}:
            return False, "XFYUN_SAMPLE_RATE_UNSUPPORTED"
        return True, ""


def _normalize_text(raw: Any) -> str:
    text = _TOKEN_RE.sub("", str(raw or "")).strip()
    return re.sub(
        r"(?<=[\u4e00-\u9fff0-9])\s+(?=[\u4e00-\u9fff0-9])",
        "",
        text,
    ).strip()


def _redacted_error(error: BaseException, provider: str) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, DialectServiceError):
        return ProviderError(
            f"XFYUN_{error.code}",
            f"科大讯飞识别失败（code={error.code}）",
            provider=provider,
        )
    if isinstance(error, asyncio.TimeoutError):
        return ProviderError(
            f"{provider.upper()}_TIMEOUT",
            f"{provider} 请求超时",
            provider=provider,
        )
    return ProviderError(
        f"{provider.upper()}_{type(error).__name__.upper()}",
        f"{provider} 连接或识别异常",
        provider=provider,
    )


class FunASRProvider:
    name = FUNASR

    def __init__(
        self,
        ws,
        *,
        call_id: str,
        segment_id: str,
        hotwords: str = "",
    ) -> None:
        self.ws = ws
        self.call_id = call_id
        self.segment_id = segment_id
        self.hotwords = hotwords
        self.started = False
        self.finished = False

    async def start(self) -> None:
        handshake = {
            "chunk_size": [5, 10, 5],
            "wav_name": f"{self.call_id}__{self.segment_id}",
            "is_speaking": True,
            "chunk_interval": 10,
            "mode": "2pass",
            "itn": True,
            "language": "auto",
            "hotwords": self.hotwords,
        }
        try:
            await self.ws.send_str(json.dumps(handshake, ensure_ascii=False))
        except Exception as exc:
            raise _redacted_error(exc, self.name) from exc
        LOG.info(
            "FunASR hotwords applied callId=%s segmentId=%s count=%d",
            self.call_id,
            self.segment_id,
            len(self.hotwords.split()),
        )
        self.started = True

    async def send_audio(self, pcm: bytes) -> None:
        if not self.started or self.finished:
            raise ProviderError("FUNASR_NOT_ACTIVE", "FunASR 语音段未启动", provider=self.name)
        try:
            await self.ws.send_bytes(pcm)
        except Exception as exc:
            raise _redacted_error(exc, self.name) from exc

    async def finish(self) -> None:
        if not self.started or self.finished:
            return
        try:
            await self.ws.send_str(json.dumps({
                "is_speaking": False,
                "mode": "2pass",
            }, ensure_ascii=False))
        except Exception as exc:
            raise _redacted_error(exc, self.name) from exc
        finally:
            self.finished = True

    async def events(self) -> AsyncIterator[ProviderResult]:
        try:
            async for message in self.ws:
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                    break
                if message.type == WSMsgType.ERROR:
                    raise ConnectionError("FunASR WebSocket error")
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    raw = json.loads(message.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                text = _normalize_text(raw.get("text", ""))
                if not text:
                    continue
                mode = str(raw.get("mode") or "streaming")
                wav_name = str(raw.get("wav_name") or "")
                prefix = f"{self.call_id}__"
                segment_id = wav_name[len(prefix):] if wav_name.startswith(prefix) else self.segment_id
                is_final = mode in _FINAL_FUNASR_MODES
                yield ProviderResult(
                    provider=self.name,
                    segment_id=segment_id or self.segment_id,
                    text=text,
                    is_final=is_final,
                    mode=mode,
                )
                if is_final:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _redacted_error(exc, self.name)
            yield ProviderResult(
                provider=self.name,
                segment_id=self.segment_id,
                error_code=error.code,
                error_message=error.message,
            )

    async def close(self) -> None:
        try:
            if not getattr(self.ws, "closed", False):
                await self.ws.close()
        except Exception:
            pass


class XfyunDialectProvider:
    name = XFYUN

    def __init__(
        self,
        client,
        config: XfyunDialectConfig,
        *,
        segment_id: str,
        frame_interval_seconds: float = 0.040,
    ) -> None:
        self.client = client
        self.config = config
        self.segment_id = segment_id
        self.frame_interval_seconds = max(0.0, float(frame_interval_seconds))
        self.ws = None
        self.started = False
        self.finished = False
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=config.queue_size)
        self._sender_task: asyncio.Task | None = None
        self._sender_error: ProviderError | None = None
        self._audio_bytes = 0

    async def start(self) -> None:
        available, reason = self.config.availability()
        if not available:
            raise ProviderUnavailableError(reason, "科大讯飞贵州话模型不可用", provider=self.name)
        auth_url = build_auth_url(
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
        )
        try:
            self.ws = await asyncio.wait_for(
                self.client.ws_connect(
                    auth_url,
                    heartbeat=20,
                    autoping=True,
                    max_msg_size=4 * 1024 * 1024,
                ),
                timeout=self.config.connect_timeout_seconds,
            )
        except Exception as exc:
            raise _redacted_error(exc, self.name) from exc
        self.started = True
        self._sender_task = asyncio.create_task(self._send_queued_audio())

    async def send_audio(self, pcm: bytes) -> None:
        if not self.started or self.finished or self.ws is None:
            raise ProviderError("XFYUN_NOT_ACTIVE", "科大讯飞语音段未启动", provider=self.name)
        self._raise_sender_error()
        payload = bytes(pcm)
        self._audio_bytes += len(payload)
        max_bytes = int(self.config.sample_rate * 2 * self.config.max_segment_seconds)
        if self._audio_bytes > max_bytes:
            raise ProviderError(
                "XFYUN_SEGMENT_TOO_LONG",
                "科大讯飞单个语音段超过安全时长",
                provider=self.name,
            )
        await self._audio_queue.put(payload)
        self._raise_sender_error()

    async def finish(self) -> None:
        if not self.started or self.finished:
            return
        self.finished = True
        await self._audio_queue.put(_QUEUE_END)
        if self._sender_task is not None:
            await self._sender_task
        self._raise_sender_error()

    async def _send_queued_audio(self) -> None:
        frame_size = frame_size_for_rate(self.config.sample_rate)
        pending = bytearray()
        seq = 0
        started_at: float | None = None
        try:
            while True:
                item = await self._audio_queue.get()
                if item is _QUEUE_END:
                    break
                pending.extend(item)
                if started_at is None:
                    # A model switch can accumulate audio while the WebSocket is
                    # connecting. Pace that backlog from the first transmitted
                    # frame instead of trying to catch up in a burst.
                    started_at = time.monotonic()
                while len(pending) >= frame_size:
                    frame = bytes(pending[:frame_size])
                    del pending[:frame_size]
                    await self._pace(started_at, seq)
                    await self._send_frame(0 if seq == 0 else 1, seq, frame)
                    seq += 1

            if started_at is None:
                started_at = time.monotonic()
            if pending:
                await self._pace(started_at, seq)
                await self._send_frame(0 if seq == 0 else 1, seq, bytes(pending))
                seq += 1
            if seq == 0:
                await self._send_frame(0, 0, b"")
                seq = 1
            await self._pace(started_at, seq)
            await self._send_frame(2, seq, b"")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._sender_error = _redacted_error(exc, self.name)

    async def _pace(self, started_at: float, frame_index: int) -> None:
        if self.frame_interval_seconds <= 0:
            return
        delay = started_at + frame_index * self.frame_interval_seconds - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _send_frame(self, status: int, seq: int, pcm: bytes) -> None:
        if self.ws is None:
            raise ProviderError("XFYUN_NOT_CONNECTED", "科大讯飞连接不存在", provider=self.name)
        message = build_audio_message(
            app_id=self.config.app_id,
            sample_rate=self.config.sample_rate,
            status=status,
            seq=seq,
            pcm=pcm,
        )
        await self.ws.send_str(json.dumps(message, ensure_ascii=False))

    def _raise_sender_error(self) -> None:
        if self._sender_error is not None:
            raise self._sender_error

    async def events(self) -> AsyncIterator[ProviderResult]:
        if self.ws is None:
            return
        accumulator = DialectResultAccumulator()
        last_text = ""
        try:
            while not accumulator.final_received:
                message = await asyncio.wait_for(
                    self.ws.receive(),
                    timeout=self.config.result_timeout_seconds,
                )
                if message.type == WSMsgType.TEXT:
                    try:
                        response = json.loads(message.data)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise DialectServiceError(
                            "invalid_json",
                            "服务端返回非 JSON 文本",
                            accumulator.sid,
                        ) from exc
                    if not isinstance(response, dict):
                        raise DialectServiceError(
                            "invalid_json",
                            "服务端返回的 JSON 不是对象",
                            accumulator.sid,
                        )
                    accumulator.consume(response)
                    text = _normalize_text(accumulator.final_text)
                    if accumulator.final_received:
                        if not text:
                            raise DialectServiceError(
                                "empty_result",
                                "服务端未返回非空识别文本",
                                accumulator.sid,
                            )
                        yield ProviderResult(
                            provider=self.name,
                            segment_id=self.segment_id,
                            text=text,
                            is_final=True,
                            mode="xfyun-offline",
                            sid=accumulator.sid,
                        )
                        return
                    if text and text != last_text:
                        last_text = text
                        yield ProviderResult(
                            provider=self.name,
                            segment_id=self.segment_id,
                            text=text,
                            is_final=False,
                            mode="xfyun-streaming",
                            sid=accumulator.sid,
                        )
                elif message.type == WSMsgType.ERROR:
                    raise ConnectionError("Xfyun WebSocket error")
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                    raise ConnectionError("Xfyun WebSocket closed before final result")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _redacted_error(exc, self.name)
            yield ProviderResult(
                provider=self.name,
                segment_id=self.segment_id,
                error_code=error.code,
                error_message=error.message,
                sid=accumulator.sid,
            )

    async def close(self) -> None:
        if self._sender_task is not None and not self._sender_task.done():
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
        try:
            if self.ws is not None and not getattr(self.ws, "closed", False):
                await self.ws.close()
        except Exception:
            pass


class ASRProviderFactory:
    """Creates one isolated provider connection per VAD segment."""

    def __init__(
        self,
        *,
        call_id: str,
        initial_funasr_ws,
        funasr_connector,
        xfyun_client=None,
        xfyun_config: XfyunDialectConfig | None = None,
    ) -> None:
        self.call_id = call_id
        self._initial_funasr_ws = initial_funasr_ws
        self._initial_funasr_consumed = False
        self._funasr_connector = funasr_connector
        self._xfyun_client = xfyun_client
        self._xfyun_config = xfyun_config or XfyunDialectConfig.from_mapping()

    def availability(self, provider_name: str) -> tuple[bool, str]:
        if provider_name == FUNASR:
            if self._initial_funasr_ws is None and self._funasr_connector is None:
                return False, "FUNASR_CONNECTOR_MISSING"
            return True, ""
        if provider_name == XFYUN:
            available, reason = self._xfyun_config.availability()
            if available and self._xfyun_client is None:
                return False, "XFYUN_CLIENT_UNAVAILABLE"
            return available, reason
        return False, "UNKNOWN_PROVIDER"

    def available_providers(self) -> list[str]:
        return [name for name in (FUNASR, XFYUN) if self.availability(name)[0]]

    async def create(self, provider_name: str, segment_id: str, *, hotwords: str = ""):
        available, reason = self.availability(provider_name)
        if not available:
            raise ProviderUnavailableError(
                reason,
                f"ASR 模型不可用：{provider_name}",
                provider=provider_name,
            )
        if provider_name == FUNASR:
            if not self._initial_funasr_consumed and self._initial_funasr_ws is not None:
                ws = self._initial_funasr_ws
                self._initial_funasr_consumed = True
            else:
                ws = await self._funasr_connector()
            provider = FunASRProvider(
                ws,
                call_id=self.call_id,
                segment_id=segment_id,
                hotwords=hotwords,
            )
        else:
            provider = XfyunDialectProvider(
                self._xfyun_client,
                self._xfyun_config,
                segment_id=segment_id,
            )
        await provider.start()
        return provider

    async def close_unused(self) -> None:
        if self._initial_funasr_consumed or self._initial_funasr_ws is None:
            return
        try:
            if not getattr(self._initial_funasr_ws, "closed", False):
                await self._initial_funasr_ws.close()
        except Exception:
            pass
        self._initial_funasr_consumed = True
