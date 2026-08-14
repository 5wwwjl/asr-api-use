#!/usr/bin/env bash
# CPU Paraformer 启动脚本。模型进程是容器 PID 1，必须启动整个容器。
set -euo pipefail

CONTAINER="funasr-paraformer-large"
HOST_PORT="10097"
MODEL_DIR="/workspace/models/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
WAIT_SECONDS="${FUNASR_START_WAIT_SECONDS:-120}"

docker start "$CONTAINER" >/dev/null

deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
    if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true \
        && docker exec "$CONTAINER" pgrep -f 'funasr_server_xhw.py' >/dev/null 2>&1; then
        echo "CPU FunASR ready: container=${CONTAINER} host_port=${HOST_PORT} model=${MODEL_DIR} ngpu=0 ncpu=4"
        exit 0
    fi
    sleep 2
done

echo "ERROR: CPU FunASR failed to become ready within ${WAIT_SECONDS}s" >&2
docker logs --tail 30 "$CONTAINER" >&2 || true
exit 1
