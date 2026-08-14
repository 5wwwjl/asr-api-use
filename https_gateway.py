import asyncio
import json
import logging
import os
import re
import ssl
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.client_exceptions import ClientConnectorError, ClientError

from asr_database import create_asr_database_reader
from asr_bridge import (
    BridgeSession,
    accept_address_scope_event,
    apply_cti_hold_event,
    broadcast_call_history,
    monitor_socket_count,
    register_monitor,
    unregister_monitor,
    start_message_service_publisher,
    close_message_service_publisher,
    force_end_all_sessions,
    active_session_model_states,
    switch_active_session_models,
    switch_paired_session_models,
    _HOTWORD_STRING,
    normalize_asr_project,
)
from asr_address_scope_rabbitmq import create_address_scope_rabbitmq_consumer
from asr_address_scope_audit import AddressScopeAuditStore
from asr_hotword_demo import (
    HotwordDemoService,
    InvalidSceneSignal,
    RealLocationDemoClient,
    RealLocationDemoError,
    transcribe_funasr,
    wav_bytes_to_pcm,
)
from hotword_manager import HotwordManager
from recording_store import DEFAULT_RECORDINGS_DIR

# FunASR 返回的语种/情绪/语音 token: <|zh|> <|NEUTRAL|> <|Speech|> 等
_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_FINAL_MODES = {"2pass-offline", "offline"}

LOG = logging.getLogger("https_gateway")

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = Path(os.getenv("GATEWAY_WEB_DIR", str(BASE_DIR / "web"))).resolve()
AUDIO_DATA_DIR = Path(
    os.getenv("GATEWAY_AUDIO_DATA_DIR", str(BASE_DIR / "audio_data"))
).resolve()


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")

GATEWAY_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8443"))
CERT_FILE = Path(os.getenv("TLS_CERT_FILE", str(BASE_DIR / "cert.pem"))).resolve()
KEY_FILE = Path(os.getenv("TLS_KEY_FILE", str(BASE_DIR / "key.pem"))).resolve()
ASR_UPSTREAM_WS = os.getenv(
    "ASR_UPSTREAM_WS",
    f"ws://127.0.0.1:{os.getenv('ASR_WS_PORT', '10094')}",
)
ASR_CPU_TEST_UPSTREAM_WS = os.getenv(
    "ASR_CPU_TEST_UPSTREAM_WS",
    "ws://127.0.0.1:10097",
)
ASR_ACCURACY_BASELINE_UPSTREAM_WS = os.getenv(
    "ASR_ACCURACY_BASELINE_UPSTREAM_WS",
    "ws://127.0.0.1:10098",
)
API_UPSTREAM = os.getenv("API_UPSTREAM", "").strip()
RECORDINGS_DIR_KEY = web.AppKey("recordings_dir", Path)
AUDIO_DATA_DIR_KEY = web.AppKey("audio_data_dir", Path)
ASR_DATABASE_READER_KEY = web.AppKey("asr_database_reader", object)
ASR_UPSTREAM_WS_KEY = web.AppKey("asr_upstream_ws", str)
ASR_CPU_TEST_UPSTREAM_WS_KEY = web.AppKey("asr_cpu_test_upstream_ws", str)
ASR_ACCURACY_BASELINE_UPSTREAM_WS_KEY = web.AppKey(
    "asr_accuracy_baseline_upstream_ws", str
)
ADDRESS_SCOPE_CONSUMER_KEY = web.AppKey("address_scope_consumer", object)
ADDRESS_SCOPE_AUDIT_STORE_KEY = web.AppKey("address_scope_audit_store", AddressScopeAuditStore)
HOTWORD_DEMO_SERVICE_KEY = web.AppKey("hotword_demo_service", HotwordDemoService)
HOTWORD_DEMO_LOCATION_CLIENT_KEY = web.AppKey(
    "hotword_demo_location_client", RealLocationDemoClient
)
_database_reader = create_asr_database_reader()


def _is_bridge_format(msg) -> bool:
    """检测首条消息是否为外部公司统一格式 (含 eventType)。"""
    if msg.type != WSMsgType.TEXT:
        return False
    try:
        return "eventType" in json.loads(msg.data)
    except json.JSONDecodeError:
        return False


def _bridge_call_id_from_first_message(first_obj: dict, fallback: str = "") -> str:
    return str(first_obj.get("callId") or first_obj.get("callid") or fallback or "").strip()


def _normalize_asr_project(value: str | None = None, *, call_id: str | None = None) -> str:
    return normalize_asr_project(value, call_id=call_id)


def _bridge_project_from_first_message(first_obj: dict, fallback: str = "") -> str:
    project = (
        first_obj.get("project")
        or first_obj.get("projectName")
        or first_obj.get("sourceProject")
        or first_obj.get("sourceSystem")
        or fallback
    )
    return _normalize_asr_project(project, call_id=_bridge_call_id_from_first_message(first_obj))


def _bridge_required_error_payload(call_id: str = "") -> dict:
    return {
        "type": "ack",
        "callId": str(call_id or "").strip(),
        "receivedSeq": 0,
        "accepted": False,
        "message": "BRIDGE_PROTOCOL_REQUIRED",
        "requiredProtocol": "bridge",
        "expectedFirstEvent": "call.started",
    }


def _print_first_client_json(raw_text: str) -> None:
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"[Client] FIRST RAW TEXT ({len(raw_text)} chars): {raw_text}", flush=True)
        return

    print("[Client] FIRST FULL JSON:", flush=True)
    print(json.dumps(obj, ensure_ascii=False, indent=2), flush=True)


def _summarize_client_text_for_log(raw_text: str) -> str:
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text
    if not isinstance(obj, dict):
        return raw_text
    if "hotwords" in obj:
        obj = dict(obj)
        obj["hotwords"] = f"<{len(str(obj['hotwords']).split())} hotwords>"
    return json.dumps(obj, ensure_ascii=False)


def _inject_hotwords_for_direct_handshake(raw_text: str) -> tuple[str, bool]:
    """Let legacy direct clients still benefit from the server hotword table."""
    if not _HOTWORD_STRING:
        return raw_text, False
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text, False
    if not isinstance(obj, dict) or "eventType" in obj:
        return raw_text, False
    if not obj.get("is_speaking"):
        return raw_text, False
    if "hotwords" in obj:
        return raw_text, False
    obj["hotwords"] = _HOTWORD_STRING
    return json.dumps(obj, ensure_ascii=False), True


def _prepare_direct_handshake(raw_text: str, *, inject_hotwords: bool) -> tuple[str, bool]:
    """Prepare a native FunASR handshake for the selected direct endpoint."""
    if not inject_hotwords:
        return raw_text, False
    return _inject_hotwords_for_direct_handshake(raw_text)


def _prepare_direct_funasr_payload(raw_data: str, state: dict) -> str | None:
    """Normalize FunASR direct output before forwarding it to old clients."""
    try:
        obj = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError):
        return raw_data

    if not isinstance(obj, dict):
        return raw_data

    obj.setdefault("callfrom", "micro")
    obj.setdefault("callto", "micro")

    if "text" in obj and isinstance(obj["text"], str):
        obj["text"] = _TOKEN_RE.sub("", obj["text"]).strip()

    mode = obj.get("mode", "")
    text = obj.get("text", "")
    if not text and state.get("sent_final_text"):
        return None

    # xhw FunASR 对所有 streaming 结果都返回 is_final=true，
    # 只用 mode 判断：2pass-offline/offline 才是真正最终结果。
    is_final = mode in _FINAL_MODES
    obj["is_final"] = is_final  # 覆盖 server 的原始值

    if is_final and text:
        state["sent_final_text"] = True

    return json.dumps(obj, ensure_ascii=False)


def _requested_hotword_mode(request: web.Request) -> str:
    """Resolve the A/B endpoint into an explicit hotword mode."""
    if request.path == "/asr-plain":
        return "off"
    if request.path == "/asr-dynamic":
        return "scene_dynamic"
    return request.query.get("hotwordMode", "").strip().lower()


async def _run_direct_relay(
    client_ws,
    upstream_ws,
    first_msg,
    *,
    inject_hotwords: bool = True,
) -> None:
    """旧格式透传：麦克风 Demo 直接把消息原样转发给 FunASR。"""
    relay_state = {"sent_final_text": False}
    if first_msg.type == WSMsgType.TEXT:
        outgoing, injected = _prepare_direct_handshake(
            first_msg.data,
            inject_hotwords=inject_hotwords,
        )
        if injected:
            print(f"[Client→ASR] FIRST TEXT hotwords injected ({len(_HOTWORD_STRING.split())} terms)", flush=True)
        print(f"[Client→ASR] FIRST TEXT: {_summarize_client_text_for_log(outgoing)}")
        await upstream_ws.send_str(outgoing)
    elif first_msg.type == WSMsgType.BINARY:
        await upstream_ws.send_bytes(first_msg.data)

    async def c2u():
        async for msg in client_ws:
            if msg.type == WSMsgType.TEXT:
                outgoing, injected = _prepare_direct_handshake(
                    msg.data,
                    inject_hotwords=inject_hotwords,
                )
                if injected:
                    print(f"[Client→ASR] TEXT hotwords injected ({len(_HOTWORD_STRING.split())} terms)", flush=True)
                print(f"[Client→ASr] TEXT: {_summarize_client_text_for_log(outgoing)}")
                await upstream_ws.send_str(outgoing)
            elif msg.type == WSMsgType.BINARY:
                await upstream_ws.send_bytes(msg.data)
            elif msg.type == WSMsgType.CLOSE:
                print(f"[Client→ASR] CLOSE")
                await upstream_ws.close()
                break

    async def u2c():
        async for msg in upstream_ws:
            if msg.type == WSMsgType.TEXT:
                data = _prepare_direct_funasr_payload(msg.data, relay_state)
                if data is None:
                    continue
                try:
                    obj = json.loads(data)
                    if isinstance(obj, dict):
                        cf = obj.get("callfrom", "micro")
                        ct = obj.get("callto", "micro")
                        print(f"[FunASR] callfrom={cf} callto={ct} {json.dumps(obj, ensure_ascii=False)}")
                except (json.JSONDecodeError, TypeError):
                    pass
                await client_ws.send_str(data)
            elif msg.type == WSMsgType.BINARY:
                await client_ws.send_bytes(msg.data)
            elif msg.type == WSMsgType.CLOSE:
                await client_ws.close()
                break

    await asyncio.gather(c2u(), u2c())


async def proxy_asr_ws(request: web.Request) -> web.WebSocketResponse:
    call_id = request.query.get("callId", "")
    call_from = request.query.get("callfrom", "")
    call_to = request.query.get("callto", "")
    project = _normalize_asr_project(request.query.get("project", ""), call_id=call_id)
    hotword_mode = _requested_hotword_mode(request)
    client_ws = web.WebSocketResponse(heartbeat=30)
    await client_ws.prepare(request)

    # 等待首条消息以判断模式
    first_msg = await client_ws.receive()
    use_bridge = _is_bridge_format(first_msg)
    is_accuracy_baseline = request.path == "/asr-accuracy-a"

    # 打印首条消息
    if first_msg.type == WSMsgType.TEXT:
        _print_first_client_json(first_msg.data)
    elif first_msg.type == WSMsgType.BINARY:
        print(f"[Client] FIRST BINARY: {len(first_msg.data)} bytes")

    # 从首条消息或 query 参数中提取 callfrom/callto
    if use_bridge:
        try:
            first_obj = json.loads(first_msg.data)
            call_from = first_obj.get("callfrom", call_from) or "micro"
            call_to = first_obj.get("callto", call_to) or "micro"
            call_id = _bridge_call_id_from_first_message(first_obj, call_id) or call_id
            project = _bridge_project_from_first_message(first_obj, project)
        except (json.JSONDecodeError, TypeError):
            pass
    elif not is_accuracy_baseline:
        print(f"[ASR] rejected non-bridge client, callId={call_id}", flush=True)
        await client_ws.send_str(json.dumps(
            _bridge_required_error_payload(call_id),
            ensure_ascii=False,
        ))
        await client_ws.close()
        return client_ws


    if is_accuracy_baseline:
        if use_bridge:
            await client_ws.send_str(json.dumps({
                "accepted": False,
                "message": "NATIVE_FUNASR_PROTOCOL_REQUIRED",
            }, ensure_ascii=False))
            await client_ws.close()
            return client_ws
        upstream_url = request.app[ASR_ACCURACY_BASELINE_UPSTREAM_WS_KEY]
        print(
            f"[ASR] endpoint=asr-accuracy-a, upstream={upstream_url}, "
            "mode=native, hotwords=off, business_postprocess=off",
            flush=True,
        )
        try:
            upstream_ws = await request.app["client"].ws_connect(
                upstream_url,
                proxy=None,
                protocols=["binary"],
            )
            await _run_direct_relay(
                client_ws,
                upstream_ws,
                first_msg,
                inject_hotwords=False,
            )
        except (ClientConnectorError, ClientError) as exc:
            print(f"[ASR] baseline upstream connect error: {exc}", flush=True)
            await client_ws.send_str(json.dumps({
                "accepted": False,
                "message": "ASR_BASELINE_UPSTREAM_UNAVAILABLE",
            }, ensure_ascii=False))
            await client_ws.close()
        return client_ws

    print(f"[ASR] client connected, callId={call_id}")
    is_cpu_test = request.path == "/asr-cpu-test"
    upstream_url = (
        request.app[ASR_CPU_TEST_UPSTREAM_WS_KEY]
        if is_cpu_test
        else request.app[ASR_UPSTREAM_WS_KEY]
    )
    endpoint_name = "asr-cpu-test" if is_cpu_test else "asr"
    print(
        f"[ASR] endpoint={endpoint_name}, upstream={upstream_url}, mode=bridge, "
        f"project={project}, callId={call_id} "
        "(callfrom/callto/speaker will be extracted from audio.frame payloads)"
    )

    try:
        async def connect_upstream():
            # 每个 VAD 段使用独立 FunASR 连接，物理隔离解码上下文。
            ws = await request.app["client"].ws_connect(
                upstream_url, proxy=None, protocols=["binary"]
            )
            if call_id:
                await ws.send_str(json.dumps({"call_id": call_id}, ensure_ascii=False))
            return ws

        if use_bridge:
            upstream_ws = await connect_upstream()
            bridge = BridgeSession(
                client_ws, upstream_ws, call_id,
                callfrom=call_from, callto=call_to,
                project=project,
                upstream_factory=connect_upstream,
                xfyun_client=request.app["client"],
                hotword_manager=(
                    HotwordManager(project=project, mode="off")
                    if hotword_mode == "off"
                    else HotwordManager(project=project, mode="scene_dynamic")
                    if hotword_mode == "scene_dynamic"
                    else None
                ),
            )
            await bridge.run(first_msg=first_msg)
    except (ClientConnectorError, ClientError) as e:
        print(f"[ASR] upstream connect error: {e}", flush=True)
        await client_ws.send_str(json.dumps({
            "type": "ack",
            "callId": call_id,
            "receivedSeq": 0,
            "accepted": False,
            "message": "ASR upstream unavailable",
        }, ensure_ascii=False))
        await client_ws.close()

    return client_ws


async def _handle_monitor_command(data: dict) -> dict | None:
    cmd = str(data.get("command", "") or data.get("type", "")).strip()
    if cmd == "force_end_all":
        count = await force_end_all_sessions()
        return {
            "type": "command_result",
            "command": "force_end_all",
            "ended": count,
        }
    if cmd == "asr.model.state":
        return {
            "event": "asr.model.state",
            "snapshot": True,
            "sessions": await active_session_model_states(),
            "sendTimeMs": int(time.time() * 1000),
        }
    if cmd == "asr.model.switch":
        request_id = str(data.get("requestId") or "").strip()
        target_provider = str(data.get("targetProvider") or "").strip().lower()
        effective = str(data.get("effective") or "").strip()
        raw_call_ids = data.get("callIds")
        call_ids = raw_call_ids if isinstance(raw_call_ids, list) else []
        call_ids = [str(call_id or "").strip() for call_id in call_ids if str(call_id or "").strip()]
        if (
            not request_id
            or not call_ids
            or target_provider not in {"funasr", "xfyun"}
            or effective != "immediate"
        ):
            return {
                "type": "command_result",
                "command": "asr.model.switch",
                "requestId": request_id,
                "accepted": False,
                "message": "INVALID_MODEL_SWITCH_COMMAND",
            }
        result = await switch_active_session_models(call_ids, target_provider, request_id)
        return {
            "type": "command_result",
            "command": "asr.model.switch",
            **result,
        }
    return None


async def monitor_ws(request: web.Request) -> web.WebSocketResponse:
    """浏览器端实时转录监控 WebSocket 端点。

    连接到 /monitor 后，接收所有 Bridge 模式通话的实时事件：
      - call.started  (新来电)
      - speech.final  (识别文本)
      - call.ended    (通话结束)
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await register_monitor(ws)
    print(f"[Monitor] client connected")
    try:
        snapshot = await _handle_monitor_command({"command": "asr.model.state"})
        if snapshot is not None:
            await ws.send_str(json.dumps(snapshot, ensure_ascii=False))
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    result = await _handle_monitor_command(data)
                    if result is not None:
                        await ws.send_str(json.dumps(result, ensure_ascii=False))
                        if result.get("command") == "force_end_all":
                            print(f"[Monitor] force_end_all: ended {result.get('ended', 0)} sessions")
                except (json.JSONDecodeError, TypeError):
                    pass  # 忽略非 JSON 消息
    finally:
        await unregister_monitor(ws)
        print(f"[Monitor] client disconnected")
    return ws


async def _asr_ws_transcribe(ws_url: str, wav_bytes: bytes, timeout: float = 30.0) -> str:
    """Send WAV audio to a FunASR WebSocket endpoint and return transcription text."""
    import struct, uuid
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.ws_connect(ws_url, protocols=["binary"]) as ws:
                # Step 1: FunASR handshake
                handshake = {
                    "chunk_size": [5, 10, 5],
                    "wav_name": f"compare__{uuid.uuid4().hex[:8]}",
                    "is_speaking": True,
                    "chunk_interval": 10,
                    "mode": "2pass",
                    "itn": True,
                }
                await ws.send_str(json.dumps(handshake, ensure_ascii=False))
                # Step 2: Send PCM audio (skip WAV header, 44 bytes)
                pcm = wav_bytes[44:]
                chunk_size = 3200  # 100ms at 16kHz mono s16le
                for i in range(0, len(pcm), chunk_size):
                    await ws.send_bytes(pcm[i:i+chunk_size])
                # Step 3: End-of-speech
                await ws.send_str(json.dumps({"is_speaking": False, "mode": "2pass"}, ensure_ascii=False))
                # Step 4: Collect results
                texts = []
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            t = data.get("text", "").strip()
                            if t:
                                texts.append(t)
                        except json.JSONDecodeError:
                            pass
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                return "".join(texts) or "(无结果)"
    except Exception as e:
        return f"(错误: {e})"


async def compare_models(request: web.Request) -> web.Response:
    """接收浏览器录音，通过 WebSocket 发送给两个 ASR 模型并返回结果。"""
    reader = await request.multipart()
    audio_bytes = None
    async for part in reader:
        if part.name == "audio":
            audio_bytes = await part.read()
            break
    if not audio_bytes:
        return web.json_response({"error": "no audio uploaded"}, status=400)

    import tempfile, subprocess, os as _os
    tmp_in = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
    tmp_in.write(audio_bytes); tmp_in.close()
    tmp_out = tmp_in.name.replace(".webm", ".wav")
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error",
            "-i", tmp_in.name, "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
            tmp_out], check=True, timeout=15)
    except Exception as e:
        _os.unlink(tmp_in.name)
        return web.json_response({"error": f"convert: {e}"}, status=500)
    _os.unlink(tmp_in.name)

    # Run standalone WebSocket clients as subprocesses (avoids event loop conflicts)
    WS_SCRIPT = os.path.join(os.path.dirname(__file__), "ws_transcribe.py")
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(subprocess.run,
                ["python3", WS_SCRIPT, "ws://192.168.173.167:10099", tmp_out],  # 原始模型 GPU
                capture_output=True, text=True, timeout=60)
            f2 = pool.submit(subprocess.run,
                ["python3", WS_SCRIPT, "ws://172.17.0.4:10097", tmp_out],  # 微调模型 GPU
                capture_output=True, text=True, timeout=60)
            r1 = f1.result(timeout=65)
            r2 = f2.result(timeout=65)
    except Exception as e:
        _os.unlink(tmp_out)
        return web.json_response({"error": str(e)}, status=500)
    _os.unlink(tmp_out)

    t1 = r1.stdout.strip() or "(无结果)"
    t2 = r2.stdout.strip() or "(无结果)"
    return web.json_response({"original": t1, "finetuned": t2})


_MODEL_SWITCH_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}


def _model_switch_response(
    *,
    success: bool,
    message: str,
    status: int,
    data: dict | None = None,
) -> web.Response:
    return web.json_response(
        {
            "success": success,
            "message": message,
            "code": status,
            "data": data,
            "timestamp": int(time.time() * 1000),
        },
        status=status,
        headers=_MODEL_SWITCH_CORS_HEADERS,
    )


async def asr_model_switch_options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_MODEL_SWITCH_CORS_HEADERS)


async def asr_model_switch(request: web.Request) -> web.Response:
    # Direct browser API: either call leg is enough to switch both audio streams.
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return _model_switch_response(
            success=False,
            message="INVALID_JSON",
            status=200,
        )
    if not isinstance(payload, dict):
        return _model_switch_response(
            success=False,
            message="INVALID_MODEL_SWITCH_REQUEST",
            status=200,
        )

    call_id = str(payload.get("callId") or "").strip()
    model = str(payload.get("model") or "").strip().lower()
    seat_id = str(payload.get("seatId") or "").strip()
    if not call_id or not seat_id or model not in {"funasr", "xfyun"}:
        return _model_switch_response(
            success=False,
            message="INVALID_MODEL_SWITCH_REQUEST",
            status=200,
        )

    request_id = uuid.uuid4().hex
    try:
        result = await switch_paired_session_models(
            call_id,
            seat_id,
            model,
            request_id,
        )
    except Exception:
        LOG.exception(
            "ASR model switch API failed requestId=%s callId=%s seatId=%s model=%s",
            request_id,
            call_id,
            seat_id,
            model,
        )
        return _model_switch_response(
            success=False,
            message="INTERNAL_SERVER_ERROR",
            status=500,
            data={"requestId": request_id},
        )

    message = str(result.get("message") or "MODEL_SWITCH_REJECTED")
    if result.get("accepted"):
        switched_call_ids = list(result.get("acceptedCallIds") or [])
        LOG.info(
            "ASR model switch API accepted requestId=%s callId=%s pairedCallIds=%s "
            "seatId=%s model=%s",
            request_id,
            call_id,
            switched_call_ids,
            seat_id,
            model,
        )
        return _model_switch_response(
            success=True,
            message="操作成功！",
            status=200,
            data={
                "requestId": request_id,
                "model": model,
                "callIds": switched_call_ids,
            },
        )

    LOG.warning(
        "ASR model switch API rejected requestId=%s callId=%s seatId=%s model=%s "
        "message=%s",
        request_id,
        call_id,
        seat_id,
        model,
        message,
    )
    return _model_switch_response(
        success=False,
        message=message,
        status=200,
        data={"requestId": request_id},
    )


async def cti_events(request: web.Request) -> web.Response:
    """Receive CTI hold/cancel events forwarded by the Java event subscriber."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"accepted": False, "message": "INVALID_JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"accepted": False, "message": "INVALID_PAYLOAD"}, status=400)

    result = await apply_cti_hold_event(payload)
    return web.json_response(result)


async def asr_records(request: web.Request) -> web.Response:
    """Return persisted ASR transcript records by callId for transfer/history replay."""
    call_id = (request.query.get("callId") or request.match_info.get("call_id") or "").strip()
    if not call_id:
        return web.json_response({"success": False, "message": "callId is required"}, status=400)

    limit_param = request.query.get("limit")
    limit = None
    if limit_param:
        try:
            limit = int(limit_param)
        except ValueError:
            return web.json_response({"success": False, "message": "limit must be an integer"}, status=400)

    reader = request.app[ASR_DATABASE_READER_KEY]
    try:
        records = await asyncio.to_thread(reader.list_records, call_id, limit=limit)
    except RuntimeError as e:
        return web.json_response({"success": False, "message": str(e)}, status=503)
    except ImportError:
        return web.json_response({"success": False, "message": "psycopg2 is not installed"}, status=503)
    except Exception:
        LOG.exception("ASR records query failed callId=%s", call_id)
        return web.json_response({"success": False, "message": "database query failed"}, status=500)

    return web.json_response({
        "success": True,
        "callId": call_id,
        "count": len(records),
        "records": records,
    }, dumps=lambda data: json.dumps(data, ensure_ascii=False))


async def push_asr_records(request: web.Request) -> web.Response:
    """Query persisted ASR records by callId and push them to monitor WebSocket clients."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"success": False, "message": "INVALID_JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"success": False, "message": "INVALID_PAYLOAD"}, status=400)

    call_id = str(payload.get("callId") or payload.get("call_id") or "").strip()
    if not call_id:
        return web.json_response({"success": False, "message": "callId is required"}, status=400)
    target_ext = str(
        payload.get("targetExt")
        or payload.get("target_ext")
        or payload.get("callto")
        or payload.get("extension")
        or ""
    ).strip()
    call_from = str(payload.get("callfrom") or payload.get("callFrom") or "").strip()

    limit = None
    if payload.get("limit") is not None:
        try:
            limit = int(payload.get("limit"))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "message": "limit must be an integer"}, status=400)

    reader = request.app[ASR_DATABASE_READER_KEY]
    try:
        records = await asyncio.to_thread(reader.list_records, call_id, limit=limit)
        event = await broadcast_call_history(
            call_id,
            records,
            callfrom=call_from,
            callto=target_ext,
        )
    except RuntimeError as e:
        return web.json_response({"success": False, "message": str(e)}, status=503)
    except ImportError:
        return web.json_response({"success": False, "message": "psycopg2 is not installed"}, status=503)
    except Exception:
        LOG.exception("ASR records push failed callId=%s", call_id)
        return web.json_response({"success": False, "message": "database query or push failed"}, status=500)

    active_monitors = monitor_socket_count()
    LOG.info(
        "ASR records pushed callId=%s callto=%s count=%s monitorCount=%s",
        call_id,
        event.get("callto", ""),
        len(records),
        active_monitors,
    )
    return web.json_response({
        "success": True,
        "pushed": True,
        "callId": call_id,
        "callto": event.get("callto", ""),
        "count": len(records),
        "monitorCount": active_monitors,
        "event": event,
    }, dumps=lambda data: json.dumps(data, ensure_ascii=False))


async def proxy_recording(request: web.Request) -> web.StreamResponse:
    """将远程 HTTP 录音文件通过本地 HTTPS 代理，解决 Mixed Content 问题。"""
    record_id = request.match_info.get("record_id", "")
    template = os.getenv("ASR_RECORDING_DOWNLOAD_URL_TEMPLATE", "").strip()
    if not template or not record_id:
        raise web.HTTPNotFound()

    remote_url = template.format(record_id=record_id, id=record_id)
    try:
        async with request.app["client"].get(remote_url) as resp:
            if resp.status >= 400:
                raise web.HTTPNotFound()
            data = await resp.read()
            out = web.Response(body=data, status=200)
            ct = resp.headers.get("Content-Type", "audio/wav")
            out.headers["Content-Type"] = ct
            out.headers["Cache-Control"] = "public, max-age=3600"
            return out
    except Exception as e:
        LOG.warning("录音代理失败 record_id=%s url=%s err=%s", record_id, remote_url, e)
        raise web.HTTPNotFound()


async def proxy_api(request: web.Request) -> web.StreamResponse:
    if not API_UPSTREAM:
        return web.json_response({"error": "API_UPSTREAM is empty"}, status=404)

    tail = request.match_info.get("tail", "")
    qs = request.query_string
    upstream_url = f"{API_UPSTREAM.rstrip('/')}/{tail}"
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    try:
        async with request.app["client"].request(
            request.method,
            upstream_url,
            data=body if body else None,
            headers=headers,
        ) as resp:
            data = await resp.read()
            out = web.Response(body=data, status=resp.status)
            for k, v in resp.headers.items():
                if k.lower() in {"content-length", "transfer-encoding", "connection", "content-encoding"}:
                    continue
                out.headers[k] = v
            return out
    except ClientConnectorError as e:
        return web.json_response({"error": "API upstream unavailable", "detail": str(e)}, status=502)


async def address_scope_audit(request: web.Request) -> web.Response:
    event_id = request.match_info.get("event_id", "").strip()
    store = request.app[ADDRESS_SCOPE_AUDIT_STORE_KEY]
    record = store.latest() if event_id == "latest" else store.get(event_id)
    if record is None:
        raise web.HTTPNotFound(text="address-scope audit record not found")
    return web.json_response(record, dumps=lambda value: json.dumps(value, ensure_ascii=False))


def _demo_json(value: object, *, status: int = 200) -> web.Response:
    return web.json_response(
        value,
        status=status,
        dumps=lambda payload: json.dumps(payload, ensure_ascii=False),
    )


async def hotword_demo_start(request: web.Request) -> web.Response:
    service = request.app[HOTWORD_DEMO_SERVICE_KEY]
    try:
        return _demo_json(service.start_session())
    except Exception as exc:
        LOG.exception("hotword demo start failed")
        return _demo_json({"error": type(exc).__name__}, status=500)


async def hotword_demo_address(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return _demo_json({"error": "INVALID_JSON"}, status=400)
    session_id = str(payload.get("sessionId") or "").strip()
    service = request.app[HOTWORD_DEMO_SERVICE_KEY]
    try:
        service.hotwords(session_id)
    except KeyError:
        return _demo_json({"error": "DEMO_SESSION_NOT_FOUND"}, status=404)

    call_id = f"hotword-demo-real-{uuid.uuid4().hex[:16]}"
    try:
        audit = await request.app[HOTWORD_DEMO_LOCATION_CLIENT_KEY].resolve(
            request.app["client"],
            call_id=call_id,
        )
        result = service.apply_address_audit(session_id, audit)
    except KeyError:
        return _demo_json({"error": "DEMO_SESSION_NOT_FOUND"}, status=404)
    except ValueError as exc:
        return _demo_json({"error": str(exc)}, status=422)
    except RealLocationDemoError as exc:
        LOG.warning("real location demo failed code=%s", exc)
        return _demo_json(
            {
                "error": "真实定位或地址小表查询失败",
                "code": str(exc),
            },
            status=502,
        )
    return _demo_json(result)


async def hotword_demo_scene(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return _demo_json({"error": "INVALID_JSON"}, status=400)
    session_id = str(payload.get("sessionId") or "").strip()
    signals = payload.get("signals")
    if not isinstance(signals, dict):
        return _demo_json({"error": "signals must be an object"}, status=400)
    try:
        result = request.app[HOTWORD_DEMO_SERVICE_KEY].apply_scene_signals(
            session_id, signals
        )
    except KeyError:
        return _demo_json({"error": "DEMO_SESSION_NOT_FOUND"}, status=404)
    except InvalidSceneSignal as exc:
        return _demo_json({"error": str(exc)}, status=422)
    return _demo_json(result)


def _convert_demo_audio_to_wav(audio_bytes: bytes) -> bytes:
    import subprocess
    import tempfile

    input_path = ""
    output_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as handle:
            handle.write(audio_bytes)
            input_path = handle.name
        output_path = f"{input_path}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", input_path,
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", output_path,
            ],
            check=True,
            timeout=20,
        )
        return Path(output_path).read_bytes()
    finally:
        for path in (input_path, output_path):
            if path:
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass


async def hotword_demo_compare(request: web.Request) -> web.Response:
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return _demo_json({"error": "multipart form required"}, status=400)
    session_id = ""
    audio_bytes = b""
    async for part in reader:
        if part.name == "sessionId":
            session_id = (await part.text()).strip()
        elif part.name == "audio":
            audio_bytes = await part.read()
    if not session_id or not audio_bytes:
        return _demo_json({"error": "sessionId and audio are required"}, status=400)
    try:
        hotwords = request.app[HOTWORD_DEMO_SERVICE_KEY].hotwords(session_id)
    except KeyError:
        return _demo_json({"error": "DEMO_SESSION_NOT_FOUND"}, status=404)

    conversion_started = time.perf_counter()
    try:
        wav_bytes = await asyncio.to_thread(_convert_demo_audio_to_wav, audio_bytes)
        pcm = wav_bytes_to_pcm(wav_bytes)
    except Exception as exc:
        LOG.warning("hotword demo audio conversion failed: %s", exc)
        return _demo_json({"error": "AUDIO_CONVERSION_FAILED"}, status=422)
    conversion_ms = round((time.perf_counter() - conversion_started) * 1000, 3)

    client = request.app["client"]
    upstream = request.app[ASR_UPSTREAM_WS_KEY]
    try:
        ordinary, dynamic = await asyncio.gather(
            transcribe_funasr(client, ws_url=upstream, pcm=pcm),
            transcribe_funasr(
                client,
                ws_url=upstream,
                pcm=pcm,
                hotwords=hotwords,
            ),
        )
    except Exception as exc:
        LOG.exception("hotword demo A/B transcription failed")
        return _demo_json({"error": type(exc).__name__}, status=502)
    return _demo_json({
        "sessionId": session_id,
        "conversionMs": conversion_ms,
        "audioDurationMs": round(len(pcm) / 32, 1),
        "ordinary": ordinary,
        "dynamic": dynamic,
    })


async def on_startup(app: web.Application) -> None:
    app["client"] = ClientSession()
    await start_message_service_publisher()
    consumer = create_address_scope_rabbitmq_consumer(
        loop=asyncio.get_running_loop(), handler=accept_address_scope_event
    )
    app[ADDRESS_SCOPE_CONSUMER_KEY] = consumer
    consumer.start()


async def on_cleanup(app: web.Application) -> None:
    consumer = app.get(ADDRESS_SCOPE_CONSUMER_KEY)
    if consumer is not None:
        await asyncio.to_thread(consumer.stop)
    await close_message_service_publisher()
    await app["client"].close()


def create_ssl_context() -> ssl.SSLContext:
    if not (CERT_FILE.exists() and KEY_FILE.exists()):
        raise FileNotFoundError(
            f"TLS cert/key not found. cert={CERT_FILE}, key={KEY_FILE}. Run generate_dev_cert.sh first."
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    context.set_alpn_protocols(["http/1.1"])  # 禁止 HTTP/2，避免 PRI/Upgrade 错误
    return context


def create_app(
    recordings_dir: Path | str | None = None,
    audio_data_dir: Path | str | None = None,
) -> web.Application:
    recording_root = Path(recordings_dir or DEFAULT_RECORDINGS_DIR).resolve()
    audio_data_root = Path(audio_data_dir or AUDIO_DATA_DIR).resolve()
    recording_root.mkdir(parents=True, exist_ok=True)
    audio_data_root.mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app[RECORDINGS_DIR_KEY] = recording_root
    app[AUDIO_DATA_DIR_KEY] = audio_data_root
    app[ASR_DATABASE_READER_KEY] = _database_reader
    app[ADDRESS_SCOPE_AUDIT_STORE_KEY] = AddressScopeAuditStore()
    app[HOTWORD_DEMO_SERVICE_KEY] = HotwordDemoService()
    app[HOTWORD_DEMO_LOCATION_CLIENT_KEY] = RealLocationDemoClient()
    app.on_startup.append(on_startup)

    # Store upstream WS URLs for /compare
    app["ASR_UPSTREAM_WS"] = ASR_UPSTREAM_WS
    app[ASR_UPSTREAM_WS_KEY] = ASR_UPSTREAM_WS
    app[ASR_CPU_TEST_UPSTREAM_WS_KEY] = ASR_CPU_TEST_UPSTREAM_WS
    app[ASR_ACCURACY_BASELINE_UPSTREAM_WS_KEY] = ASR_ACCURACY_BASELINE_UPSTREAM_WS
    app["ASR_FINETUNED_WS"] = "ws://172.17.0.4:10098"
    app.on_cleanup.append(on_cleanup)

    app.router.add_post("/compare", compare_models)
    app.router.add_get("/asr", proxy_asr_ws)
    app.router.add_get("/asr-plain", proxy_asr_ws)
    app.router.add_get("/asr-dynamic", proxy_asr_ws)
    app.router.add_get("/asr-cpu-test", proxy_asr_ws)
    app.router.add_get("/asr-accuracy-a", proxy_asr_ws)
    app.router.add_get("/asr/records", asr_records)
    app.router.add_post("/asr/records/push", push_asr_records)
    app.router.add_get("/asr/transcripts/{call_id}", asr_records)
    app.router.add_get("/api/address-scope-audit/{event_id}", address_scope_audit)
    app.router.add_post("/api/hotword-demo/start", hotword_demo_start)
    app.router.add_post("/api/hotword-demo/address", hotword_demo_address)
    app.router.add_post("/api/hotword-demo/scene", hotword_demo_scene)
    app.router.add_post("/api/hotword-demo/compare", hotword_demo_compare)
    app.router.add_get("/monitor", monitor_ws)
    app.router.add_post("/asr/model/switch", asr_model_switch)
    app.router.add_options("/asr/model/switch", asr_model_switch_options)
    app.router.add_post("/cti/events", cti_events)
    app.router.add_get("/audio/{record_id}", proxy_recording)
    if API_UPSTREAM:
        app.router.add_route("*", "/api/{tail:.*}", proxy_api)
    app.router.add_static("/recordings/", str(recording_root), show_index=False)
    app.router.add_static("/audio_data/", str(audio_data_root), show_index=False)
    app.router.add_static("/", str(WEB_DIR), show_index=True)
    return app


if __name__ == "__main__":
    ssl_context = create_ssl_context()
    app = create_app()
    print(f"HTTPS gateway running at https://{GATEWAY_HOST}:{GATEWAY_PORT}/index.html")
    print(f"ASR upstream: {ASR_UPSTREAM_WS}")
    print(f"ASR CPU test endpoint: /asr-cpu-test -> {ASR_CPU_TEST_UPSTREAM_WS}")
    print(
        "ASR accuracy A endpoint: /asr-accuracy-a -> "
        f"{ASR_ACCURACY_BASELINE_UPSTREAM_WS}"
    )
    print(f"API upstream: {API_UPSTREAM or '(disabled)'}")
    web.run_app(app, host=GATEWAY_HOST, port=GATEWAY_PORT, ssl_context=ssl_context)
