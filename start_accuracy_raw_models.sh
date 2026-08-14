#!/usr/bin/env bash
# Start isolated no-VAD A/C evaluation models without changing online 10098/10099.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${ASR_RAW_EVAL_IMAGE:-funasr-xhw-runtime:gpu-ngc25.06}"
MODEL_HOST_ROOT="${FUNASR_MODEL_HOST_ROOT:-/home/twai/xhw/FunASR/server/funasr-runtime-resources/models}"
A_MODEL_REL="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
C_MODEL_REL="damo/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404"
A_CONTAINER="funasr-accuracy-raw-a"
C_CONTAINER="funasr-accuracy-raw-c"
A_PORT="${ASR_RAW_A_PORT:-10101}"
C_PORT="${ASR_RAW_C_PORT:-10102}"
WAIT_SECONDS="${ASR_RAW_WAIT_SECONDS:-240}"

for model in "$A_MODEL_REL" "$C_MODEL_REL"; do
    if [[ ! -f "$MODEL_HOST_ROOT/$model/model.pt" ]]; then
        echo "ERROR: model is missing: $MODEL_HOST_ROOT/$model/model.pt" >&2
        exit 1
    fi
done

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: image is missing: $IMAGE" >&2
    exit 1
fi

create_container() {
    local name="$1" port="$2" model_rel="$3"
    if docker inspect "$name" >/dev/null 2>&1; then
        local command configured_port
        command="$(docker inspect -f '{{join .Config.Cmd " "}}' "$name")"
        configured_port="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "10095/tcp") 0).HostPort}}' "$name")"
        if [[ "$command" != *"--disable_vad"* || "$configured_port" != "$port" ]]; then
            echo "ERROR: stale evaluation container exists: $name" >&2
            exit 1
        fi
        return
    fi
    docker create \
        --name "$name" \
        --restart unless-stopped \
        --gpus all \
        --ipc host \
        --ulimit memlock=-1 \
        --ulimit stack=67108864 \
        --security-opt label=disable \
        -p "$port:10095" \
        -v "$MODEL_HOST_ROOT:/workspace/models:ro" \
        -v "$BASE_DIR/funasr_server_xhw.py:/workspace/funasr_server_xhw.py:ro" \
        "$IMAGE" \
        /bin/bash -lc \
        "cd /workspace && exec python funasr_server_xhw.py --host 0.0.0.0 --port 10095 --model_dir /workspace/models/$model_rel --disable_vad --ngpu 1 --ncpu 4 --disable_update" \
        >/dev/null
}

create_container "$A_CONTAINER" "$A_PORT" "$A_MODEL_REL"
create_container "$C_CONTAINER" "$C_PORT" "$C_MODEL_REL"
docker start "$A_CONTAINER" "$C_CONTAINER" >/dev/null

websocket_ready() {
    local port="$1"
    /home/twai/anaconda3/bin/python3.13 - "$port" <<'PY' >/dev/null 2>&1
import asyncio
import sys
import websockets

async def main():
    async with websockets.connect(
        f"ws://127.0.0.1:{sys.argv[1]}",
        subprotocols=["binary"],
        open_timeout=2,
    ):
        return

asyncio.run(main())
PY
}

deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
    if websocket_ready "$A_PORT" && websocket_ready "$C_PORT"; then
        echo "Raw no-VAD A ready: ws://127.0.0.1:$A_PORT"
        echo "Raw no-VAD C ready: ws://127.0.0.1:$C_PORT"
        exit 0
    fi
    sleep 2
done

echo "ERROR: raw evaluation models failed readiness within ${WAIT_SECONDS}s" >&2
docker logs --tail 30 "$A_CONTAINER" >&2 || true
docker logs --tail 30 "$C_CONTAINER" >&2 || true
exit 1
