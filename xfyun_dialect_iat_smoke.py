#!/usr/bin/env python3
"""科大讯飞方言识别大模型 WebAPI 独立冒烟客户端。

凭证仅从环境变量读取：
  XFYUN_APP_ID
  XFYUN_API_KEY
  XFYUN_API_SECRET
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp


HOST = "iat.cn-huabei-1.xf-yun.com"
REQUEST_PATH = "/v1"
BASE_WS_URL = f"wss://{HOST}{REQUEST_PATH}"
FRAME_INTERVAL_SECONDS = 0.040
SUPPORTED_SAMPLE_RATES = {8000, 16000}
MAX_AUDIO_SECONDS = 60.0


class ConfigurationError(ValueError):
    """本地调用配置错误。"""


class UnsupportedAudioError(ValueError):
    """输入 WAV 不符合方言模型要求。"""


class DialectServiceError(RuntimeError):
    """科大讯飞方言模型返回的脱敏服务错误。"""

    def __init__(self, code: str | int, description: str, sid: str = "") -> None:
        self.code = str(code)
        self.description = str(description or "服务端返回异常")
        self.sid = str(sid or "")
        super().__init__(self.description)

    def __str__(self) -> str:
        sid_part = f", sid={self.sid}" if self.sid else ""
        return f"code={self.code}, desc={self.description}{sid_part}"


@dataclass(frozen=True)
class WavAudio:
    path: Path
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int
    duration_seconds: float


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    sid: str
    elapsed_seconds: float


def frame_size_for_rate(sample_rate: int) -> int:
    """返回单声道 s16le 音频 40ms 对应的字节数。"""
    if sample_rate not in SUPPORTED_SAMPLE_RATES:
        raise UnsupportedAudioError(f"不支持的采样率：{sample_rate}")
    return int(sample_rate * 2 * FRAME_INTERVAL_SECONDS)


def read_wav_audio(path: str | Path) -> WavAudio:
    """读取不超过60秒的8k/16k单声道16bit PCM WAV。"""
    wav_path = Path(path)
    if not wav_path.is_file():
        raise FileNotFoundError(f"音频文件不存在：{wav_path}")

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()

            if compression != "NONE":
                raise UnsupportedAudioError("只支持未压缩 PCM WAV")
            if channels != 1:
                raise UnsupportedAudioError(f"只支持单声道 WAV，当前为 {channels} 声道")
            if sample_width != 2:
                raise UnsupportedAudioError(
                    f"只支持 16bit WAV，当前位深为 {sample_width * 8}bit"
                )
            if sample_rate not in SUPPORTED_SAMPLE_RATES:
                raise UnsupportedAudioError(
                    f"只支持 8000/16000Hz WAV，当前为 {sample_rate}Hz"
                )

            duration_seconds = frame_count / sample_rate if sample_rate else 0.0
            if duration_seconds <= 0:
                raise UnsupportedAudioError("音频内容为空")
            if duration_seconds > MAX_AUDIO_SECONDS:
                raise UnsupportedAudioError(
                    f"方言模型单次音频最长60秒，当前为 {duration_seconds:.3f}秒"
                )
            pcm = wav_file.readframes(frame_count)
    except wave.Error as exc:
        raise UnsupportedAudioError("无法解析 WAV 文件") from exc

    return WavAudio(
        path=wav_path,
        pcm=pcm,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        duration_seconds=duration_seconds,
    )


def build_auth_url(
    *,
    api_key: str,
    api_secret: str,
    now: datetime | None = None,
) -> str:
    """按照方言模型协议生成 HMAC-SHA256 鉴权 URL。"""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    date_header = format_datetime(current_time.astimezone(timezone.utc), usegmt=True)
    signature_origin = (
        f"host: {HOST}\n"
        f"date: {date_header}\n"
        f"GET {REQUEST_PATH} HTTP/1.1"
    )
    signature_digest = hmac.new(
        api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_digest).decode("ascii")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(
        authorization_origin.encode("utf-8")
    ).decode("ascii")
    return f"{BASE_WS_URL}?{urlencode({'authorization': authorization, 'date': date_header, 'host': HOST})}"


def build_audio_message(
    *,
    app_id: str,
    sample_rate: int,
    status: int,
    seq: int,
    pcm: bytes,
) -> dict[str, Any]:
    """构造方言模型首帧、中间帧或结束帧。"""
    frame_size_for_rate(sample_rate)
    if status not in (0, 1, 2):
        raise ValueError(f"非法音频状态：{status}")
    message: dict[str, Any] = {
        "header": {"status": status, "app_id": app_id},
        "payload": {
            "audio": {
                "encoding": "raw",
                "sample_rate": sample_rate,
                "channels": 1,
                "bit_depth": 16,
                "status": status,
                "seq": seq,
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        },
    }
    if status == 0:
        message["parameter"] = {
            "iat": {
                "language": "zh_cn",
                "accent": "mulacc",
                "domain": "slm",
                "dwa": "wpgs",
                "result": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "json",
                },
            }
        }
    return message


def _segment_sort_key(segment_id: Any) -> tuple[int, int | str]:
    try:
        return 0, int(segment_id)
    except (TypeError, ValueError):
        return 1, str(segment_id)


def _extract_words(result: dict[str, Any]) -> str:
    words: list[str] = []
    ws_items = result.get("ws", [])
    for ws_item in ws_items if isinstance(ws_items, list) else []:
        candidates = ws_item.get("cw", []) if isinstance(ws_item, dict) else []
        if candidates and isinstance(candidates[0], dict):
            word = candidates[0].get("w")
            if word is not None:
                words.append(str(word))
    return "".join(words)


class DialectResultAccumulator:
    """解码响应并应用 wpgs 的追加/替换语义。"""

    def __init__(self) -> None:
        self._segments: dict[Any, str] = {}
        self.final_received = False
        self.sid = ""

    def consume(self, response: dict[str, Any]) -> None:
        header = response.get("header")
        if not isinstance(header, dict):
            raise DialectServiceError("invalid_response", "响应缺少 header")

        code = header.get("code", 0)
        self.sid = str(header.get("sid") or self.sid)
        if code not in (0, "0", None):
            raise DialectServiceError(
                code,
                str(header.get("message") or "服务端返回异常"),
                self.sid,
            )
        if header.get("status") == 2:
            self.final_received = True

        payload = response.get("payload")
        result_payload = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result_payload, dict) or not result_payload.get("text"):
            return

        if result_payload.get("compress", "raw") != "raw":
            raise DialectServiceError(
                "unsupported_compress",
                f"不支持的结果压缩格式：{result_payload.get('compress')}",
                self.sid,
            )
        try:
            decoded = base64.b64decode(result_payload["text"], validate=True)
            result = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DialectServiceError(
                "invalid_result",
                "无法解码服务端识别结果",
                self.sid,
            ) from exc
        if not isinstance(result, dict):
            raise DialectServiceError("invalid_result", "识别结果不是 JSON 对象", self.sid)

        segment_id = result.get("sn", result_payload.get("seq", len(self._segments)))
        if result.get("pgs") == "rpl":
            replace_range = result.get("rg")
            if isinstance(replace_range, list) and len(replace_range) == 2:
                try:
                    start, end = int(replace_range[0]), int(replace_range[1])
                except (TypeError, ValueError):
                    start = end = -1
                for key in list(self._segments):
                    try:
                        numeric_key = int(key)
                    except (TypeError, ValueError):
                        continue
                    if start <= numeric_key <= end:
                        self._segments.pop(key, None)
        self._segments[segment_id] = _extract_words(result)

    @property
    def final_text(self) -> str:
        return "".join(
            self._segments[key]
            for key in sorted(self._segments, key=_segment_sort_key)
        )


def public_error_message(error: BaseException) -> str:
    """生成不会回显鉴权 URL 和凭证的错误信息。"""
    if isinstance(error, (ConfigurationError, UnsupportedAudioError, DialectServiceError)):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return str(error)
    if isinstance(error, aiohttp.ClientResponseError):
        return f"{type(error).__name__}(status={error.status})"
    if isinstance(error, asyncio.TimeoutError):
        return "TimeoutError"
    return type(error).__name__


async def _receive_results(
    ws: aiohttp.ClientWebSocketResponse,
    accumulator: DialectResultAccumulator,
) -> None:
    while not accumulator.final_received:
        message = await ws.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            try:
                response = json.loads(message.data)
            except json.JSONDecodeError as exc:
                raise DialectServiceError(
                    "invalid_json", "服务端返回非 JSON 文本", accumulator.sid
                ) from exc
            if not isinstance(response, dict):
                raise DialectServiceError(
                    "invalid_json", "服务端返回的 JSON 不是对象", accumulator.sid
                )
            accumulator.consume(response)
        elif message.type == aiohttp.WSMsgType.ERROR:
            raise ConnectionError("WebSocket 接收失败")
        elif message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        ):
            raise ConnectionError("WebSocket 在最终结果返回前关闭")


async def _send_audio(
    ws: aiohttp.ClientWebSocketResponse,
    audio: WavAudio,
    *,
    app_id: str,
    receiver_task: asyncio.Task[None],
) -> None:
    frame_size = frame_size_for_rate(audio.sample_rate)
    frame_count = (len(audio.pcm) + frame_size - 1) // frame_size
    started_at = time.monotonic()

    for frame_index, offset in enumerate(range(0, len(audio.pcm), frame_size)):
        if receiver_task.done():
            await receiver_task
        target_time = started_at + frame_index * FRAME_INTERVAL_SECONDS
        delay = target_time - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        status = 0 if frame_index == 0 else 1
        message = build_audio_message(
            app_id=app_id,
            sample_rate=audio.sample_rate,
            status=status,
            seq=frame_index,
            pcm=audio.pcm[offset : offset + frame_size],
        )
        await ws.send_str(json.dumps(message, ensure_ascii=False))

    final_delay = started_at + frame_count * FRAME_INTERVAL_SECONDS - time.monotonic()
    if final_delay > 0:
        await asyncio.sleep(final_delay)
    final_message = build_audio_message(
        app_id=app_id,
        sample_rate=audio.sample_rate,
        status=2,
        seq=frame_count,
        pcm=b"",
    )
    await ws.send_str(json.dumps(final_message, ensure_ascii=False))


async def transcribe(
    audio: WavAudio,
    *,
    app_id: str,
    api_key: str,
    api_secret: str,
    connect_timeout: float = 15.0,
    result_timeout: float = 30.0,
) -> TranscriptionResult:
    """真实调用科大讯飞方言识别大模型并返回最终文本。"""
    auth_url = build_auth_url(api_key=api_key, api_secret=api_secret)
    started_at = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=None, connect=connect_timeout)
    accumulator = DialectResultAccumulator()

    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
        async with client.ws_connect(
            auth_url,
            heartbeat=20,
            autoping=True,
            max_msg_size=4 * 1024 * 1024,
        ) as ws:
            receiver_task = asyncio.create_task(_receive_results(ws, accumulator))
            try:
                await _send_audio(
                    ws,
                    audio,
                    app_id=app_id,
                    receiver_task=receiver_task,
                )
                await asyncio.wait_for(receiver_task, timeout=result_timeout)
            finally:
                if not receiver_task.done():
                    receiver_task.cancel()
                    await asyncio.gather(receiver_task, return_exceptions=True)

    final_text = accumulator.final_text.strip()
    if not final_text:
        raise DialectServiceError(
            "empty_result",
            "服务端未返回非空识别文本",
            accumulator.sid,
        )
    return TranscriptionResult(
        text=final_text,
        sid=accumulator.sid,
        elapsed_seconds=time.monotonic() - started_at,
    )


def _credentials_from_environment() -> tuple[str, str, str]:
    names = ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET")
    values = tuple(os.environ.get(name, "").strip() for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ConfigurationError(f"缺少环境变量：{', '.join(missing)}")
    return values  # type: ignore[return-value]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="科大讯飞方言识别大模型 WebAPI 冒烟调用")
    parser.add_argument("wav", type=Path, help="不超过60秒的8k/16k单声道16bit PCM WAV")
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--result-timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        app_id, api_key, api_secret = _credentials_from_environment()
        audio = read_wav_audio(args.wav)
        print(
            "调用配置："
            f"audio={audio.path}, sampleRate={audio.sample_rate}, "
            f"duration={audio.duration_seconds:.3f}s, domain=slm, accent=mulacc",
            flush=True,
        )
        result = asyncio.run(
            transcribe(
                audio,
                app_id=app_id,
                api_key=api_key,
                api_secret=api_secret,
                connect_timeout=args.connect_timeout,
                result_timeout=args.result_timeout,
            )
        )
    except KeyboardInterrupt:
        print("调用取消", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"调用失败：{public_error_message(exc)}", file=sys.stderr)
        return 1

    print("调用成功")
    if result.sid:
        print(f"sid={result.sid}")
    print(f"elapsed={result.elapsed_seconds:.3f}s")
    print(f"text={result.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
