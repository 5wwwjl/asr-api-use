#!/usr/bin/env python3
"""
本地 ASR 代理 — 解决浏览器代理拦截问题。

浏览器  --ws-->  127.0.0.1:8765  --wss-->  sqasr.telewave.com.cn:8443
                  (本脚本)                   (绕过代理直连)
"""
import asyncio
import json
import os
import ssl
import sys
from pathlib import Path

import websockets

UPSTREAM = "wss://sqasr.telewave.com.cn:8443/asr"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765

CERT_FILE = Path(__file__).resolve().parent / "cert.pem"


async def relay(client_ws, call_id):
    """将客户端 WebSocket 消息中继到上游 WSS，绕过系统代理。"""
    # 直连 IP + 指定 SNI，完全绕过代理
    ctx = ssl.create_default_context()
    try:
        upstream_ws = await websockets.connect(
            UPSTREAM + f"?callId={call_id}",
            ssl=ctx,
            ping_interval=None,
            open_timeout=8,
        )
    except Exception as e:
        print(f"[proxy] 上游连接失败: {e}")
        try:
            await client_ws.send(json.dumps({"mode": "error", "text": f"上游不可达: {e}", "is_final": True}, ensure_ascii=False))
        except Exception:
            pass
        return

    print(f"[proxy] 已连接上游, callId={call_id}")

    async def c2u():
        try:
            async for msg in client_ws:
                if isinstance(msg, bytes):
                    await upstream_ws.send(msg)
                elif isinstance(msg, str):
                    await upstream_ws.send(msg)
        except websockets.ConnectionClosed:
            pass

    async def u2c():
        try:
            async for msg in upstream_ws:
                if isinstance(msg, bytes):
                    await client_ws.send(msg)
                elif isinstance(msg, str):
                    await client_ws.send(msg)
        except websockets.ConnectionClosed:
            pass

    await asyncio.gather(c2u(), u2c(), return_exceptions=True)

    try:
        await upstream_ws.close()
    except Exception:
        pass
    print(f"[proxy] 会话结束, callId={call_id}")


async def handler(client_ws):
    """处理每个浏览器客户端连接。"""
    # 从路径中提取 callId
    path = client_ws.request.path if hasattr(client_ws, 'request') else "/"
    call_id = "demo_local"
    if "callId=" in path:
        call_id = path.split("callId=")[-1].split("&")[0]

    print(f"[proxy] 浏览器已连接")
    await relay(client_ws, call_id)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LISTEN_PORT
    print(f"ASR 本地代理: ws://{LISTEN_HOST}:{port}/asr")
    print(f"上游: {UPSTREAM}")
    print("浏览器打开 asr_demo_local.html 即可使用")
    print()

    async with websockets.serve(handler, LISTEN_HOST, port, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
