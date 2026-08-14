"""
Fun-ASR 流式识别服务
支持: SenseVoiceSmall, VAD, ITN, 热词, WebSocket
"""

import asyncio
import json
import logging
import ssl
import traceback
from argparse import ArgumentParser

import websockets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

parser = ArgumentParser()
parser.add_argument("--host", type=str, default="0.0.0.0", help="服务地址")
parser.add_argument("--port", type=int, default=10095, help="服务端口")
parser.add_argument("--model_dir", type=str, default="/home/twai/xhw/model/iic/SenseVoiceSmall", help="模型路径")
parser.add_argument("--vad_model", type=str, default="fsmn-vad", help="VAD模型")
parser.add_argument("--vad_kwargs", type=str, default='{"max_single_segment_time": 30000}', help="VAD参数(JSON格式)")
parser.add_argument("--disable_vad", action="store_true", help="禁用模型内置VAD，直接识别完整输入音频")
parser.add_argument("--ngpu", type=int, default=1, help="GPU数量, 0表示CPU")
parser.add_argument("--ncpu", type=int, default=4, help="CPU核心数")
parser.add_argument("--fp16", action="store_true", help="使用 FP16 权重降低 GPU 显存占用")
parser.add_argument("--disable_update", action="store_true", help="禁用模型更新检查")
args = parser.parse_args()
args.vad_kwargs = json.loads(args.vad_kwargs)

websocket_users = set()
model = None
rich_transcription_postprocess = None
# AutoModel uses one shared CUDA model. Keep GPU calls serial, but execute the
# blocking generate() call in a worker thread so handshakes, audio receive and
# disconnects on every WebSocket can continue while inference is running.
inference_lock = asyncio.Lock()
MAX_DEFERRED_PARTIALS = 1


def _hotword_count(hotwords) -> int:
    """Return the number of whitespace-separated hotwords in a handshake string."""
    if not hotwords:
        return 0
    return len(str(hotwords).strip().split())


def load_model():
    """加载 ASR 模型"""
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    logger.info(f"Loading model from: {args.model_dir}")
    vad_options = {}
    if not args.disable_vad:
        vad_options = {
            "vad_model": args.vad_model,
            "vad_kwargs": args.vad_kwargs,
        }
    logger.info("VAD: %s", "disabled" if args.disable_vad else args.vad_model)
    asr_model = AutoModel(
        model=args.model_dir,
        trust_remote_code=True,
        device="cuda" if args.ngpu > 0 else "cpu",
        ngpu=args.ngpu,
        ncpu=args.ncpu,
        fp16=args.fp16,
        disable_pbar=True,
        disable_log=True,
        disable_update=args.disable_update,
        **vad_options,
    )
    logger.info("Model loaded successfully!")
    return asr_model, rich_transcription_postprocess


async def ws_serve(websocket):
    """处理 WebSocket 连接"""
    frames_asr = []
    vad_pre_idx = 0
    chunk_interval = 10
    wav_name = "microphone"
    is_speaking = False
    cache = {}
    deferred_partial_count = 0
    partial_result_count = 0

    websocket_users.add(websocket)
    logger.info(f"New user connected. Total users: {len(websocket_users)}")

    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    messagejson = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if "is_speaking" in messagejson:
                    was_speaking = is_speaking
                    is_speaking = bool(messagejson["is_speaking"])
                    if was_speaking and not is_speaking:
                        audio_in = b"".join(frames_asr)
                        if audio_in:
                            try:
                                # The bridge ends a segment with a JSON control
                                # message, not an extra binary frame. Flush here
                                # so the final result is emitted immediately.
                                await async_asr_streaming(
                                    websocket,
                                    audio_in,
                                    wav_name,
                                    False,
                                    cache,
                                )
                            except Exception as e:
                                logger.error(f"ASR final inference error: {e}")
                                traceback.print_exc()
                        frames_asr = []
                        cache = {}
                        deferred_partial_count = 0
                        partial_result_count = 0

                if "chunk_interval" in messagejson:
                    chunk_interval = messagejson["chunk_interval"]

                if "wav_name" in messagejson:
                    wav_name = messagejson.get("wav_name")

                if "hotwords" in messagejson:
                    cache["hotwords"] = messagejson["hotwords"]
                    if not cache.get("_hotwords_logged"):
                        logger.info("Received %d hotwords from handshake", _hotword_count(cache["hotwords"]))
                        cache["_hotwords_logged"] = True

                if "language" in messagejson:
                    cache["language"] = messagejson["language"]

            elif not isinstance(message, str):
                duration_ms = len(message) // 32
                vad_pre_idx += duration_ms
                frames_asr.append(message)

                if len(frames_asr) % chunk_interval == 0 or not is_speaking:
                    # A cumulative partial supersedes every older partial for
                    # this utterance. Do not queue obsolete GPU work behind a
                    # busy model; the next interval (or mandatory final frame)
                    # will contain all audio collected so far.
                    if (
                        is_speaking
                        and partial_result_count > 0
                        and deferred_partial_count < MAX_DEFERRED_PARTIALS
                        and inference_lock.locked()
                    ):
                        deferred_partial_count += 1
                        if deferred_partial_count == 1 or deferred_partial_count % 10 == 0:
                            logger.info(
                                "Deferred %d obsolete partial inference(s) for %s",
                                deferred_partial_count,
                                wav_name,
                            )
                        continue

                    audio_in = b"".join(frames_asr)
                    if len(audio_in) > 0:
                        try:
                            await async_asr_streaming(websocket, audio_in, wav_name, is_speaking, cache)
                            deferred_partial_count = 0
                            if is_speaking:
                                partial_result_count += 1
                        except Exception as e:
                            logger.error(f"ASR streaming error: {e}")
                            traceback.print_exc()

                    if not is_speaking:
                        frames_asr = []
                        cache = {}
                        deferred_partial_count = 0
                        partial_result_count = 0

    except websockets.ConnectionClosed:
        logger.info(f"Connection closed. Users remaining: {len(websocket_users)}")
    except websockets.InvalidState:
        logger.error("Invalid websocket state")
    except Exception as e:
        logger.error(f"Exception: {e}")
        traceback.print_exc()
    finally:
        websocket_users.discard(websocket)


async def async_asr_streaming(websocket, audio_in, wav_name, is_speaking, cache):
    """流式 ASR 推理"""
    if len(audio_in) == 0:
        return

    try:
        generate_kwargs = {
            "input": audio_in,
            "cache": cache,
            "batch_size_s": 60,
            "use_itn": True,
            "language": "auto",
        }
        if not args.disable_vad:
            generate_kwargs.update({"merge_vad": True, "merge_length_s": 15})
        hotwords = cache.get("hotwords")
        if hotwords:
            # FunASR contextual/seaco Paraformer reads the runtime hotword list
            # from the `hotword` generate kwarg, not from the streaming cache.
            generate_kwargs["hotword"] = hotwords

        queued_at = asyncio.get_running_loop().time()
        async with inference_lock:
            queue_ms = (asyncio.get_running_loop().time() - queued_at) * 1000
            inference_started = asyncio.get_running_loop().time()
            res = await asyncio.to_thread(model.generate, **generate_kwargs)
            inference_ms = (asyncio.get_running_loop().time() - inference_started) * 1000
        logger.info(
            "Inference completed wav=%s audioMs=%d queueMs=%.1f inferenceMs=%.1f",
            wav_name,
            len(audio_in) // 32,
            queue_ms,
            inference_ms,
        )

        if res and len(res) > 0:
            text = rich_transcription_postprocess(res[0]["text"])

            result_msg = {
                "mode": "streaming" if is_speaking else "2pass-offline",
                "text": text,
                "wav_name": wav_name,
                "is_final": not is_speaking,
            }

            await websocket.send(json.dumps(result_msg, ensure_ascii=False))
            logger.info(f"Sent: {text[:50]}...")

    except Exception as e:
        logger.error(f"ASR inference error: {e}")
        traceback.print_exc()


async def main():
    """启动服务"""
    global model, rich_transcription_postprocess

    logger.info(f"Starting Fun-ASR Server on {args.host}:{args.port}")
    logger.info(f"Model: {args.model_dir}")
    precision = "FP16" if args.fp16 else "FP32"
    logger.info(f"Using {'GPU' if args.ngpu > 0 else 'CPU'} / {precision}")

    model, rich_transcription_postprocess = load_model()

    async with websockets.serve(
        ws_serve,
        args.host,
        args.port,
        subprotocols=["binary"],
        ping_interval=None,
        # Dynamic address scopes can contain tens of thousands of hotwords.
        # The default 1 MiB WebSocket limit rejects the initial handshake.
        max_size=None,
    ):
        logger.info("Server started successfully!")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
