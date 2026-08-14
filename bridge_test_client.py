#!/usr/bin/env python3
"""
Bridge 协议测试客户端
模拟外部公司向 wss://<host>:8443/asr 推送语音流 (call.started → audio.frame → call.ended)

用法:
  python bridge_test_client.py <音频文件>                     # 自动 ffmpeg 转 PCM
  python bridge_test_client.py <音频文件> --raw               # 16kHz PCM 裸流
  echo "你好世界" | python bridge_test_client.py -            # TTS 合成后推送
  python bridge_test_client.py "我是报警人" --text            # 直接文本 TTS
"""
import argparse
import asyncio
import base64
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

DEFAULT_ENDPOINT = "wss://sqasr.telewave.com.cn:8443/asr"


def ensure_pcm(filepath: str) -> bytes:
    """将任意音频文件转为 16kHz mono 16bit PCM。"""
    if filepath.endswith(".pcm") or filepath.endswith(".raw"):
        return Path(filepath).read_bytes()

    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
        out = tmp.name

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", filepath, "-ar", "16000", "-ac", "1", "-f", "s16le", out],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"音频转换失败: {result.stderr}")

    data = Path(out).read_bytes()
    os.unlink(out)
    return data


def tts_to_pcm(text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> bytes:
    """用 edge-tts 将文本合成语音并转为 PCM。"""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3 = tmp.name
    subprocess.run(
        ["python3", "-m", "edge_tts", "--voice", voice, "--text", text, "--write-media", mp3],
        capture_output=True, text=True, check=True,
    )
    pcm = ensure_pcm(mp3)
    os.unlink(mp3)
    return pcm


async def run_bridge_test(
    ws,
    pcm: bytes,
    call_id: str,
    callfrom: str,
    callto: str,
    chunk_ms: int = 100,
    verbose: bool = True,
    speaker: str = "caller",
    qa_mode: bool = False,
    qa_chunks: int = 20,
):
    """按 Bridge 协议推送音频流: call.started → audio.frame × N → call.ended。

    qa_mode=True 时，前半段音频设为 agent，后半段设为 caller，
    模拟"接警员提问 → 报警人回答"的问答场景。
    """
    now_ms = lambda: int(time.time() * 1000)
    bytes_per_chunk = 32000 * chunk_ms // 1000

    # 预计算所有 chunk 和对应的 speaker
    chunks = []
    for i in range(0, len(pcm), bytes_per_chunk):
        chunk = pcm[i:i + bytes_per_chunk]
        if chunk:
            chunks.append(chunk)

    if qa_mode and len(chunks) >= 2:
        mid = max(1, len(chunks) // 2)
        chunk_speakers = ["agent"] * mid + ["caller"] * (len(chunks) - mid)
    else:
        chunk_speakers = [speaker] * len(chunks)

    # 1. call.started
    seq = 1
    started = {
        "schemaVersion": "1.0",
        "eventId": f"evt-{call_id}-{seq:06d}",
        "eventType": "call.started",
        "callId": call_id,
        "streamId": "stream-main",
        "seq": seq,
        "timestampMs": 0,
        "sendTimeMs": now_ms(),
        "sourceSystem": "bridge-test-client",
        "payload": {},
    }
    await ws.send(json.dumps(started, ensure_ascii=False))
    if verbose:
        print(f"  → call.started  callId={call_id} (qa_mode={qa_mode})")

    ack = json.loads(await ws.recv())
    if verbose:
        print(f"  ← ack  accepted={ack.get('accepted')}")

    # 2. audio.frame × N
    total_ms = 0
    total_bytes = 0
    last_speaker = None
    results = []  # 提前初始化，发送阶段就可能收到 speech.final
    for idx, chunk in enumerate(chunks):
        seq += 1
        spk = chunk_speakers[idx]
        if spk != last_speaker and verbose:
            print(f"  🎤 speaker → {spk} ({speakerLabel(spk)})")
            last_speaker = spk

        frame_ms = len(chunk) * 1000 // 32000
        total_ms += frame_ms
        total_bytes += len(chunk)
        direction = "outbound" if spk == "agent" else "inbound"

        frame = {
            "schemaVersion": "1.0",
            "eventId": f"evt-{call_id}-{seq:06d}",
            "eventType": "audio.frame",
            "callId": call_id,
            "streamId": "stream-main",
            "seq": seq,
            "timestampMs": total_ms,
            "sendTimeMs": now_ms(),
            "sourceSystem": "bridge-test-client",
            "payload": {
                "speaker": spk,
                "direction": direction,
                "startTimeMs": total_ms - frame_ms,
                "endTimeMs": total_ms,
                "codec": "pcm_s16le",
                "sampleRate": 16000,
                "channels": 1,
                "frameDurationMs": frame_ms,
                "audioBase64": base64.b64encode(chunk).decode("ascii"),
                "callfrom": callfrom,
                "callto": callto,
            },
        }
        await ws.send(json.dumps(frame, ensure_ascii=False))
        # 收集所有响应 (ack + speech.final)，不丢弃 speech.final
        try:
            while True:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.1))
                if resp.get("eventType") == "speech.final":
                    text = resp.get("payload", {}).get("text", "")
                    spk = resp.get("payload", {}).get("speaker", "")
                    if text:
                        results.append(text)
                        if verbose:
                            print(f"  ← speech.final [{spk}]: 「{text}」")
                elif resp.get("type") == "ack":
                    if verbose and seq % 10 == 0:
                        print(f"  ← ack  seq={seq}  accepted={resp.get('accepted')}")
                    break  # ack 收到，继续发送下一帧
        except asyncio.TimeoutError:
            pass  # 无消息，继续
        await asyncio.sleep(chunk_ms / 1000)

    if verbose:
        print(f"  已发送: {total_bytes / 1000:.1f} KB  ({total_ms / 1000:.1f} 秒)")

    # 3. call.ended
    seq += 1
    ended = {
        "schemaVersion": "1.0",
        "eventId": f"evt-{call_id}-{seq:06d}",
        "eventType": "call.ended",
        "callId": call_id,
        "streamId": "stream-main",
        "seq": seq,
        "timestampMs": total_ms,
        "sendTimeMs": now_ms(),
        "sourceSystem": "bridge-test-client",
        "payload": {},
    }
    await ws.send(json.dumps(ended, ensure_ascii=False))
    if verbose:
        print(f"  → call.ended")

    try:
        ack = json.loads(await ws.recv())
        if verbose:
            print(f"  ← ack  accepted={ack.get('accepted')}")
    except Exception:
        pass

    # 收剩余 speech.final 结果
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            obj = json.loads(msg)
            if obj.get("eventType") == "speech.final":
                text = obj.get("payload", {}).get("text", "")
                results.append(text)
                if verbose:
                    print(f"  ← speech.final: 「{text}」")
            elif obj.get("type") == "ack":
                continue
            else:
                if verbose:
                    print(f"  ← ? {json.dumps(obj, ensure_ascii=False)[:120]}")
    except asyncio.TimeoutError:
        pass
    except websockets.ConnectionClosed:
        pass

    return results


def speakerLabel(spk: str) -> str:
    return {"agent": "接警员", "caller": "报警人", "system": "系统"}.get(spk, spk)


async def main():
    parser = argparse.ArgumentParser(
        description="Bridge 协议测试客户端 — 模拟外部公司推送语音流",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s recording.wav                          # 音频文件测试
  %(prog)s recording.pcm --raw                    # 裸 PCM 测试
  echo "着火了" | %(prog)s -                       # stdin TTS 测试
  %(prog)s "深圳大学南区三栋" --text               # 直接文本 TTS
  %(prog)s call.wav --call-id fire-001 --insecure  # 指定 callId + 跳过 TLS 验证
        """,
    )
    parser.add_argument("input", help="音频文件路径，'-' 从 stdin 读文本，或直接文本配合 --text")
    parser.add_argument("--raw", action="store_true", help="输入是 16kHz PCM 裸数据")
    parser.add_argument("--text", action="store_true", help="输入是文本，自动 edge-tts 合成")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"Gateway 地址 (默认: {DEFAULT_ENDPOINT})")
    parser.add_argument("--call-id", default=None, help="通话唯一标识 (默认自动生成)")
    parser.add_argument("--callfrom", default="13800138000", help="主叫号码")
    parser.add_argument("--callto", default="119", help="被叫号码")
    parser.add_argument("--chunk-ms", type=int, default=100, help="每块音频毫秒数 (默认: 100)")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="TTS 音色")
    parser.add_argument("--speaker", default="caller", choices=["caller", "agent", "system"], help="说话人角色 (默认: caller)")
    parser.add_argument("--qa", action="store_true", help="QA 模式: 前半段 agent 提问 + 后半段 caller 回答")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书验证 (dev 自签证书)")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终识别结果")
    args = parser.parse_args()

    # 生成 callId
    if args.call_id is None:
        args.call_id = f"test-{int(time.time())}"

    # 准备 PCM
    if args.input == "-":
        text = sys.stdin.read().strip()
        if not args.quiet:
            print(f"TTS 合成: 「{text}」...")
        pcm = tts_to_pcm(text, args.voice)
    elif args.text:
        if not args.quiet:
            print(f"TTS 合成: 「{args.input}」...")
        pcm = tts_to_pcm(args.input, args.voice)
    elif args.raw:
        pcm = Path(args.input).read_bytes()
        if not args.quiet:
            print(f"PCM: {len(pcm) / 1000:.1f} KB ({len(pcm) / 32000:.2f} 秒)")
    else:
        if not args.quiet:
            print(f"转换音频: {args.input}")
        pcm = ensure_pcm(args.input)
        if not args.quiet:
            print(f"PCM: {len(pcm) / 1000:.1f} KB ({len(pcm) / 32000:.2f} 秒)")

    if len(pcm) == 0:
        print("错误: 音频数据为空", file=sys.stderr)
        sys.exit(1)

    # 连接
    url = f"{args.endpoint}?callId={args.call_id}"
    if not args.quiet:
        print(f"连接: {url}")
        print(f"callId={args.call_id}  callfrom={args.callfrom}  callto={args.callto}")

    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(url, ssl=ctx, ping_interval=None, open_timeout=10) as ws:
            if not args.quiet:
                print("已连接\n")
            results = await run_bridge_test(
                ws, pcm, args.call_id,
                callfrom=args.callfrom, callto=args.callto,
                chunk_ms=args.chunk_ms, verbose=not args.quiet,
                speaker=args.speaker, qa_mode=args.qa,
            )
    except Exception as e:
        print(f"连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 输出最终结果
    if results:
        full_text = " ".join(results)
        if args.quiet:
            print(full_text)
        else:
            print(f"\n最终识别 ({len(results)} 段): {full_text}")
    elif not args.quiet:
        print("(未收到 speech.final — FunASR 上游可能不可达)")


if __name__ == "__main__":
    asyncio.run(main())
