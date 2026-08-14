#!/usr/bin/env python3
"""
轻量级 Mock FunASR 服务器 — 用于测试 ASR 链路，无需真实 FunASR。
接受 PCM 音频帧，返回模拟的识别文本。
"""
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("需要 pip install websockets", file=sys.stderr)
    sys.exit(1)

MOCK_TEXTS = [
    "你好，这里是119接警中心，请问有什么情况？",
    "我在中山路四十五号，三楼有浓烟冒出来。",
    "对，就是朝阳小区三号楼。",
    "三楼右边那个房间，窗户能看到黑烟。",
    "有人被困在里面，我不确定几个人。",
]


async def handle_asr(ws):
    """接收 FunASR 协议消息，返回模拟识别结果。"""
    print(f"[mock-funasr] client connected")
    is_speaking = False
    text_idx = 0

    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                # PCM audio frame — silently consume
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            if "is_speaking" in data:
                is_speaking = data.get("is_speaking", False)
                print(f"[mock-funasr] is_speaking={is_speaking}")

                if is_speaking:
                    # 发送在线结果
                    await ws.send(json.dumps({
                        "mode": "2pass-online",
                        "text": "正在识别中...",
                        "is_final": False,
                        "wav_name": data.get("wav_name", ""),
                    }, ensure_ascii=False))
                    text_idx = 0
                else:
                    # 发送离线最终结果
                    text = MOCK_TEXTS[min(text_idx, len(MOCK_TEXTS) - 1)]
                    await ws.send(json.dumps({
                        "mode": "2pass-offline",
                        "text": text,
                        "is_final": True,
                        "wav_name": data.get("wav_name", ""),
                    }, ensure_ascii=False))
                    print(f"[mock-funasr] final text: {text}")
    except websockets.ConnectionClosed:
        pass
    print(f"[mock-funasr] client disconnected")


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10096
    print(f"[mock-funasr] listening on ws://0.0.0.0:{port}")
    async with websockets.serve(handle_asr, "0.0.0.0", port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
