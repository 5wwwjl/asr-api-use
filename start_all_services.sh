#!/bin/bash
# ASR 全栈服务启动脚本 — 开机自启 + 进程守护
# 用法: ./start_all_services.sh  (crontab @reboot 或手动)
set -e

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-/home/twai/anaconda3/bin/python3.13}"
mkdir -p "$LOG_DIR"

# Prevent the minute-level watchdog from racing a deliberate full restart.
exec 9>/tmp/asr-service-maintenance.lock
flock 9

echo "[$(date '+%F %T')] ===== ASR 服务启动 =====" | tee -a "$LOG_DIR/startup.log"

# Default boot state keeps both GPU A and GPU E online for the realtime demo.
rm -f /tmp/asr-accuracy-baseline.active

# ── 1. GPU ContextualParaformer (生产 /asr) ─────────
GPU_CONTAINER="funasr-paraformer-large-gpu"
echo "[$(date '+%F %T')] 检查 GPU ContextualParaformer (Docker)..." | tee -a "$LOG_DIR/startup.log"

if ! docker inspect "$GPU_CONTAINER" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] ERROR: GPU 容器不存在: $GPU_CONTAINER" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

if ! docker start "$GPU_CONTAINER" >> "$LOG_DIR/startup.log" 2>&1; then
    echo "[$(date '+%F %T')] ERROR: GPU ContextualParaformer 启动失败" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

gpu_start_deadline=$((SECONDS + 120))
while (( SECONDS < gpu_start_deadline )); do
    if docker inspect -f '{{.State.Running}}' "$GPU_CONTAINER" 2>/dev/null | grep -qx true \
        && docker exec "$GPU_CONTAINER" pgrep -f 'funasr_server_xhw.py' >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if docker inspect -f '{{.State.Running}}' "$GPU_CONTAINER" 2>/dev/null | grep -qx true \
    && docker exec "$GPU_CONTAINER" pgrep -f 'funasr_server_xhw.py' >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] GPU ContextualParaformer 已就绪 (端口 10099)" | tee -a "$LOG_DIR/startup.log"
else
    echo "[$(date '+%F %T')] ERROR: GPU ContextualParaformer 未在120秒内就绪" | tee -a "$LOG_DIR/startup.log"
    docker logs --tail 20 "$GPU_CONTAINER" 2>&1 | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

# ── 2. CPU Paraformer (测试 /asr-cpu-test) ──────────
echo "[$(date '+%F %T')] 启动 GPU Paraformer-large (Docker, /asr-accuracy-a)..." | tee -a "$LOG_DIR/startup.log"
if "$BASE_DIR/start_funasr_gpu_baseline.sh" >> "$LOG_DIR/startup.log" 2>&1; then
    echo "[$(date '+%F %T')] GPU Paraformer-large A 组启动成功 (端口 10098)" | tee -a "$LOG_DIR/startup.log"
else
    echo "[$(date '+%F %T')] ERROR: GPU Paraformer-large A 组启动失败" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

echo "[$(date '+%F %T')] 启动 CPU Paraformer (Docker, /asr-cpu-test)..." | tee -a "$LOG_DIR/startup.log"

if "$BASE_DIR/start_funasr_docker.sh" >> "$LOG_DIR/startup.log" 2>&1; then
    echo "[$(date '+%F %T')] CPU Paraformer 启动成功" | tee -a "$LOG_DIR/startup.log"
else
    echo "[$(date '+%F %T')] ERROR: CPU Paraformer 启动失败" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

# ── 3. Gateway ───────────────────────────────────────
echo "[$(date '+%F %T')] 启动 Gateway..." | tee -a "$LOG_DIR/startup.log"

# 杀掉旧 Gateway
pkill -f "https_gateway.py" 2>/dev/null || true

gateway_stop_deadline=$((SECONDS + 15))
while (( SECONDS < gateway_stop_deadline )); do
    if ! pgrep -f "https_gateway.py" >/dev/null 2>&1 \
        && ! ss -lnt 2>/dev/null | awk '$4 ~ /:8443$/ { found=1 } END { exit !found }'; then
        break
    fi
    sleep 0.2
done

if pgrep -f "https_gateway.py" >/dev/null 2>&1 \
    || ss -lnt 2>/dev/null | awk '$4 ~ /:8443$/ { found=1 } END { exit !found }'; then
    echo "[$(date '+%F %T')] WARN: 旧 Gateway 未在15秒内优雅退出，执行强制关闭" | tee -a "$LOG_DIR/startup.log"
    pkill -KILL -f "[h]ttps_gateway.py" 2>/dev/null || true
fi

gateway_force_deadline=$((SECONDS + 5))
while (( SECONDS < gateway_force_deadline )); do
    if ! pgrep -f "https_gateway.py" >/dev/null 2>&1 \
        && ! ss -lnt 2>/dev/null | awk '$4 ~ /:8443$/ { found=1 } END { exit !found }'; then
        break
    fi
    sleep 0.2
done

if pgrep -f "https_gateway.py" >/dev/null 2>&1 \
    || ss -lnt 2>/dev/null | awk '$4 ~ /:8443$/ { found=1 } END { exit !found }'; then
    echo "[$(date '+%F %T')] ERROR: 无法关闭旧 Gateway" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

# Give the kernel a brief gap after the old listener is gone.
sleep 1

# 绕过代理启动 Gateway
cd "$BASE_DIR"
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  no_proxy="*" NO_PROXY="*" PYTHONUNBUFFERED=1 \
  nohup "$PYTHON_BIN" https_gateway.py 9>&- > "$LOG_DIR/gateway.log" 2>&1 &
gateway_pid=$!

gateway_pid_owns_port() {
    kill -0 "$gateway_pid" 2>/dev/null || return 1
    ss -lntp 2>/dev/null | grep ':8443' | grep -q "pid=${gateway_pid},"
}

gateway_ready=0
gateway_start_deadline=$((SECONDS + 30))
while (( SECONDS < gateway_start_deadline )); do
    if ! kill -0 "$gateway_pid" 2>/dev/null; then
        break
    fi
    if gateway_pid_owns_port; then
        sleep 2
        if gateway_pid_owns_port; then
            gateway_ready=1
            break
        fi
    fi
    sleep 0.5
done

if [[ "$gateway_ready" -eq 1 ]]; then
    echo "[$(date '+%F %T')] Gateway 启动成功 (PID $gateway_pid, 端口 8443 已稳定2秒)" | tee -a "$LOG_DIR/startup.log"
else
    echo "[$(date '+%F %T')] ERROR: Gateway 启动失败或端口不属于新进程 PID $gateway_pid" | tee -a "$LOG_DIR/startup.log"
    tail -10 "$LOG_DIR/gateway.log" | tee -a "$LOG_DIR/startup.log"
    exit 1
fi

echo "[$(date '+%F %T')] ===== 启动完成 =====" | tee -a "$LOG_DIR/startup.log"
echo "  ASR:      ws://192.168.173.167:10099 (GPU ContextualParaformer, production /asr)"
echo "  Baseline: ws://192.168.173.167:10098 (GPU Paraformer-large, realtime accuracy A group)"
echo "  CPU test: ws://192.168.173.167:10097 (CPU Paraformer, /asr-cpu-test)"
echo "  Gateway: wss://sqasr.telewave.com.cn:8443"
echo "  Monitor: https://<IP>:8443/monitor.html"
