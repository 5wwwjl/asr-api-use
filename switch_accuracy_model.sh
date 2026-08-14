#!/usr/bin/env bash
# Switch the single GB10 between the A baseline and E production model.
# Accuracy evaluation is sequential: save all A outputs, restore E, then replay
# exactly the same audio list through the full business chain.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-}"
PRODUCTION_CONTAINER="funasr-paraformer-large-gpu"
BASELINE_CONTAINER="funasr-paraformer-large-gpu-baseline"
WAIT_SECONDS="${FUNASR_START_WAIT_SECONDS:-180}"
BASELINE_MARKER="/tmp/asr-accuracy-baseline.active"
ROLLBACK_BASELINE=0

if [[ "$TARGET" != "baseline" && "$TARGET" != "production" ]]; then
    echo "Usage: $0 baseline|production" >&2
    exit 2
fi

exec 8>/tmp/asr-accuracy-model-switch.lock
flock 8

websocket_ready() {
    local url="$1"
    python3 - "$url" <<'PY' >/dev/null 2>&1
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

wait_for_model() {
    local container="$1"
    local url="$2"
    local label="$3"
    local deadline=$((SECONDS + WAIT_SECONDS))
    while (( SECONDS < deadline )); do
        if docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -qx true \
            && docker exec "$container" pgrep -f 'funasr_server_xhw.py' >/dev/null 2>&1 \
            && websocket_ready "$url"; then
            echo "$label ready: $url"
            return 0
        fi
        sleep 2
    done
    echo "ERROR: $label failed to become ready within ${WAIT_SECONDS}s" >&2
    docker logs --tail 40 "$container" >&2 || true
    return 1
}

rollback_baseline_switch() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ "$ROLLBACK_BASELINE" -eq 1 ]]; then
        set +e
        echo "Baseline switch was interrupted or failed; restoring production E." >&2
        rm -f "$BASELINE_MARKER"
        docker stop -t 5 "$BASELINE_CONTAINER" >/dev/null 2>&1
        docker start "$PRODUCTION_CONTAINER" >/dev/null
        wait_for_model "$PRODUCTION_CONTAINER" "ws://127.0.0.1:10099" "GPU ContextualParaformer production"
        docker update --restart=unless-stopped "$PRODUCTION_CONTAINER" >/dev/null
    fi
    exit "$status"
}

trap rollback_baseline_switch EXIT HUP INT TERM

if [[ "$TARGET" == "baseline" ]]; then
    echo "Switching GPU to A: Paraformer-large baseline. Production E will be temporarily unavailable."
    touch "$BASELINE_MARKER"
    ROLLBACK_BASELINE=1
    docker update --restart=no "$PRODUCTION_CONTAINER" >/dev/null
    docker stop -t 20 "$PRODUCTION_CONTAINER" >/dev/null
    if ! "$BASE_DIR/start_funasr_gpu_baseline.sh"; then
        echo "Baseline startup failed." >&2
        exit 1
    fi
    ROLLBACK_BASELINE=0
    echo "A is active on ws://127.0.0.1:10098. Run the baseline dataset and save its outputs before restoring E."
    exit 0
fi

echo "Restoring GPU to E: ContextualParaformer production."
rm -f "$BASELINE_MARKER"
docker update --restart=no "$BASELINE_CONTAINER" >/dev/null 2>&1 || true
docker stop -t 20 "$BASELINE_CONTAINER" >/dev/null 2>&1 || true
docker start "$PRODUCTION_CONTAINER" >/dev/null
wait_for_model "$PRODUCTION_CONTAINER" "ws://127.0.0.1:10099" "GPU ContextualParaformer production"
docker update --restart=unless-stopped "$PRODUCTION_CONTAINER" >/dev/null
echo "E is active again. Replay the identical audio list through /asr."
