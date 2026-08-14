#!/usr/bin/env bash
# GPU Paraformer-large accuracy baseline (A group).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="funasr-paraformer-large-gpu-baseline"
HOST_PORT="${ASR_GPU_BASELINE_PORT:-10098}"
IMAGE="${ASR_GPU_BASELINE_IMAGE:-funasr-xhw-runtime:gpu-ngc25.06}"
MODEL_HOST_ROOT="${FUNASR_MODEL_HOST_ROOT:-/home/twai/xhw/FunASR/server/funasr-runtime-resources/models}"
MODEL_RELATIVE_DIR="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
MODEL_DIR="/workspace/models/${MODEL_RELATIVE_DIR}"
VAD_RELATIVE_DIR="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
VAD_DIR="/workspace/models/${VAD_RELATIVE_DIR}"
WAIT_SECONDS="${FUNASR_START_WAIT_SECONDS:-180}"
PRODUCTION_CONTAINER="funasr-paraformer-large-gpu"

if docker inspect -f '{{.State.Running}}' "$PRODUCTION_CONTAINER" 2>/dev/null | grep -qx true; then
    echo "Production E is running; attempting FP32 GPU baseline coexistence."
fi

if [[ ! -f "${MODEL_HOST_ROOT}/${MODEL_RELATIVE_DIR}/model.pt" ]]; then
    echo "ERROR: GPU baseline model is missing: ${MODEL_HOST_ROOT}/${MODEL_RELATIVE_DIR}/model.pt" >&2
    exit 1
fi

if [[ ! -f "${MODEL_HOST_ROOT}/${VAD_RELATIVE_DIR}/model.pt" ]]; then
    echo "ERROR: local VAD model is missing: ${MODEL_HOST_ROOT}/${VAD_RELATIVE_DIR}/model.pt" >&2
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: GPU baseline image is missing: $IMAGE" >&2
    exit 1
fi

if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    configured_command="$(docker inspect -f '{{join .Config.Cmd " "}}' "$CONTAINER")"
    if [[ "$configured_command" == *"--fp16"* \
        || "$configured_command" != *"$VAD_DIR"* ]]; then
        if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
            echo "ERROR: $CONTAINER is running with stale precision settings; stop it before recreation." >&2
            exit 1
        fi
        docker rm "$CONTAINER" >/dev/null
    fi
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker create \
        --name "$CONTAINER" \
        --restart no \
        --gpus all \
        --ipc host \
        --ulimit memlock=-1 \
        --ulimit stack=67108864 \
        --security-opt label=disable \
        -p "${HOST_PORT}:10095" \
        -v "${MODEL_HOST_ROOT}:/workspace/models" \
        -v "${BASE_DIR}/funasr_server_xhw.py:/workspace/funasr_server_xhw.py:ro" \
        "$IMAGE" \
        /bin/bash -lc \
        "cd /workspace && sed -i \"s/if len(cache) == 0:/if 'prev_samples' not in cache:/\" /workspace/FunASR/funasr/models/fsmn_vad_streaming/model.py 2>/dev/null || true; exec python funasr_server_xhw.py --host 0.0.0.0 --port 10095 --model_dir ${MODEL_DIR} --vad_model ${VAD_DIR} --vad_kwargs '{\"max_single_segment_time\": 30000}' --ngpu 1 --ncpu 4 --disable_update" \
        >/dev/null
fi

configured_port="$(
    docker inspect -f '{{(index (index .HostConfig.PortBindings "10095/tcp") 0).HostPort}}' \
        "$CONTAINER" 2>/dev/null || true
)"
if [[ "$configured_port" != "$HOST_PORT" ]]; then
    echo "ERROR: $CONTAINER publishes port ${configured_port:-unknown}, expected ${HOST_PORT}" >&2
    exit 1
fi

docker start "$CONTAINER" >/dev/null

websocket_ready() {
    python3 - "ws://127.0.0.1:${HOST_PORT}" <<'PY' >/dev/null 2>&1
import asyncio
import sys

import aiohttp

async def main():
    timeout = aiohttp.ClientTimeout(total=2)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(sys.argv[1], protocols=["binary"]):
            return

asyncio.run(main())
PY
}

deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
    if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true \
        && docker exec "$CONTAINER" pgrep -f 'funasr_server_xhw.py' >/dev/null 2>&1 \
        && websocket_ready; then
        docker update --restart=unless-stopped "$CONTAINER" >/dev/null
        echo "GPU Paraformer baseline ready: container=${CONTAINER} host_port=${HOST_PORT} model=${MODEL_DIR} ngpu=1 ncpu=4 precision=fp32"
        exit 0
    fi
    sleep 2
done

echo "ERROR: GPU Paraformer baseline failed to become ready within ${WAIT_SECONDS}s" >&2
docker logs --tail 40 "$CONTAINER" >&2 || true
exit 1
