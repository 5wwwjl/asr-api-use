#!/bin/bash
# ASR 服务看门狗 — 每分钟检查服务存活，挂了自动拉起
# crontab: * * * * * /home/twai/huilong/full_question_v6_strata/asr_api_use/watchdog.sh

LOG="/home/twai/huilong/full_question_v6_strata/asr_api_use/logs/watchdog.log"
PYTHON_BIN="${PYTHON_BIN:-/home/twai/anaconda3/bin/python3.13}"

# A deliberate full restart owns this lock; skip this watchdog tick to avoid races.
exec 9>/tmp/asr-service-maintenance.lock
flock -n 9 || exit 0

# ── Gateway 检查 ──
if ! ss -lntp 2>/dev/null | grep -q ':8443'; then
    echo "[$(date '+%F %T')] Gateway 挂了，重启中..." >> "$LOG"
    cd /home/twai/huilong/full_question_v6_strata/asr_api_use
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
      no_proxy="*" NO_PROXY="*" PYTHONUNBUFFERED=1 \
      nohup "$PYTHON_BIN" https_gateway.py 9>&- > logs/gateway.log 2>&1 &
fi

# ── GPU ContextualParaformer (生产 /asr) 检查 ──
GPU_CONTAINER="funasr-paraformer-large-gpu"
if [[ ! -f /tmp/asr-accuracy-baseline.active ]]; then
    if ! docker inspect -f '{{.State.Running}}' "$GPU_CONTAINER" 2>/dev/null | grep -qx true \
        || ! docker exec "$GPU_CONTAINER" pgrep -f 'funasr_server_xhw.py' > /dev/null 2>&1; then
        echo "[$(date '+%F %T')] GPU ContextualParaformer 挂了，重启容器中..." >> "$LOG"
        docker restart "$GPU_CONTAINER" >> "$LOG" 2>&1 || true
    fi
fi

# ── GPU Paraformer-large (实时 /asr-accuracy-a) 检查 ──
BASELINE_CONTAINER="funasr-paraformer-large-gpu-baseline"
if ! docker inspect -f '{{.State.Running}}' "$BASELINE_CONTAINER" 2>/dev/null | grep -qx true \
    || ! docker exec "$BASELINE_CONTAINER" pgrep -f 'funasr_server_xhw.py' > /dev/null 2>&1; then
    echo "[$(date '+%F %T')] GPU Paraformer-large A 组挂了，重启容器中..." >> "$LOG"
    cd /home/twai/huilong/full_question_v6_strata/asr_api_use
    FUNASR_START_WAIT_SECONDS=180 ./start_funasr_gpu_baseline.sh >> "$LOG" 2>&1 || true
fi

# ── CPU Paraformer (测试 /asr-cpu-test) 检查 ──
CONTAINER="funasr-paraformer-large"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true \
    || ! docker exec "$CONTAINER" pgrep -f 'funasr_server_xhw.py' > /dev/null 2>&1; then
    echo "[$(date '+%F %T')] CPU Paraformer 挂了，重启容器中..." >> "$LOG"
    docker restart "$CONTAINER" >> "$LOG" 2>&1 || true
fi
