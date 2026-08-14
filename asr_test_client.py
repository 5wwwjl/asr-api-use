#!/usr/bin/env python3
"""
ASR 接口测试客户端
模拟第三方语音厂商向 wss://sqasr.telewave.com.cn:8443/asr 推送音频流

用法:
  python asr_test_client.py <音频文件>              # 自动识别格式
  python asr_test_client.py <音频文件> --raw        # 16kHz PCM 裸流
  echo "你好世界" | python asr_test_client.py -     # TTS 合成后推送
"""
import argparse
import asyncio
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

ENDPOINT = "wss://sqasr.telewave.com.cn:8443/asr"


def ensure_pcm(filepath: str) -> bytes:
    """将任意音频文件转为 16kHz mono 16bit PCM。"""
    if filepath.endswith(".pcm") or filepath.endswith(".raw"):
        return Path(filepath).read_bytes()

    # 用 ffmpeg 转
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


async def send_asr(ws, pcm: bytes, chunk_ms: int = 100, verbose: bool = True):
    """推送音频流并等待识别结果。"""
    # 1. FunASR 握手
    handshake = json.dumps({
        "chunk_size": [5, 10, 5],
        "wav_name": "test_client",
        "is_speaking": True,
        "chunk_interval": 10,
        "mode": "2pass",
        "itn": True,
        "language": "auto",
    })
    await ws.send(handshake)

    # 2. 分块发送音频
    bytes_per_chunk = 32000 * chunk_ms // 1000  # 16kHz 16bit = 32000 bytes/s
    total = 0
    for i in range(0, len(pcm), bytes_per_chunk):
        chunk = pcm[i:i + bytes_per_chunk]
        await ws.send(chunk)
        total += len(chunk)
        await asyncio.sleep(chunk_ms / 1000)

    if verbose:
        print(f"已发送: {total / 1000:.1f} KB  ({total / 32000:.2f} 秒)")

    # 3. 结束信号
    await ws.send(json.dumps({"is_speaking": False, "mode": "2pass"}))
    if verbose:
        print("已发送结束信号，等待结果...\n")

    # 4. 收结果
    results = []
    try:
        async for msg in ws:
            obj = json.loads(msg)
            results.append(obj)
            if verbose:
                marker = " [FINAL]" if obj.get("is_final") or obj.get("mode") == "2pass-offline" else ""
                print(f"  <<< {obj.get('text', '')}{marker}")
            if obj.get("is_final") or obj.get("mode") == "2pass-offline":
                break
    except websockets.ConnectionClosed:
        pass

    return results


async def main():
    parser = argparse.ArgumentParser(description="ASR 接口测试客户端")
    parser.add_argument("input", help="音频文件路径，或 '-' 从 stdin 读取文本后 TTS，或直接输入文本")
    parser.add_argument("--raw", action="store_true", help="输入是 16kHz PCM 裸数据")
    parser.add_argument("--text", action="store_true", help="输入是文本，自动 TTS 合成")
    parser.add_argument("--endpoint", default=ENDPOINT, help=f"ASR 服务地址 (默认: {ENDPOINT})")
    parser.add_argument("--call-id", default="test_client", help="会话标识")
    parser.add_argument("--chunk-ms", type=int, default=100, help="每块音频毫秒数 (默认: 100)")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="TTS 音色")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终识别结果")
    args = parser.parse_args()

    # 准备 PCM 数据
    if args.input == "-":
        text = sys.stdin.read().strip()
    elif args.text:
        text = args.input
    else:
        text = ""

    if args.input == "-" or args.text:
        if not args.quiet:
            print(f"TTS 合成: 「{text}」...")
        pcm = tts_to_pcm(text, args.voice)
        if not args.quiet:
            print(f"PCM: {len(pcm) / 1000:.1f} KB ({len(pcm) / 32000:.2f} 秒)")
    elif args.raw:
        pcm = Path(args.input).read_bytes()
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

    ctx = ssl.create_default_context()
    try:
        async with websockets.connect(url, ssl=ctx, ping_interval=None, open_timeout=10) as ws:
            if not args.quiet:
                print("已连接\n")
            results = await send_asr(ws, pcm, args.chunk_ms, verbose=not args.quiet)
    except Exception as e:
        print(f"连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 输出最终结果
    final = [r for r in results if r.get("is_final") or r.get("mode") == "2pass-offline"]
    if final:
        text = " ".join(r.get("text", "") for r in final)
        if args.quiet:
            print(text)
        else:
            print(f"\n最终识别: {text}")
    elif not args.quiet:
        print("(未收到最终结果)")


if __name__ == "__main__":
    asyncio.run(main())
