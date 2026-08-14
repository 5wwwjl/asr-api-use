#!/usr/bin/env python3
"""Standalone WebSocket ASR client. Usage: python3 ws_transcribe.py <ws_url> <wav_file>"""
import asyncio, aiohttp, json, sys, uuid

async def transcribe(ws_url, wav_path):
    with open(wav_path, 'rb') as f:
        wav = f.read()
    pcm = wav[44:]

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url, protocols=["binary"]) as ws:
            await ws.send_str(json.dumps({
                "chunk_size": [5, 10, 5],
                "wav_name": f"cmp_{uuid.uuid4().hex[:8]}",
                "is_speaking": True, "chunk_interval": 10, "mode": "2pass",
            }))
            for i in range(0, len(pcm), 3200):
                await ws.send_bytes(pcm[i:i+3200])
            await ws.send_str(json.dumps({"is_speaking": False, "mode": "2pass"}))
            texts = []
            try:
                async with asyncio.timeout(15):
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            t = data.get("text", "").strip()
                            if t: texts.append(t)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break
            except TimeoutError:
                pass
            return max(texts, key=len) if texts else "(无结果)"

if __name__ == "__main__":
    result = asyncio.run(transcribe(sys.argv[1], sys.argv[2]))
    print(result)
