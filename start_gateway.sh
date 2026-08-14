#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f cert.pem || ! -f key.pem ]]; then
  ./generate_dev_cert.sh
fi

# 从 .env 读取上游地址
if [[ -f .env ]]; then
  ASR_UPSTREAM=$(grep -oP '^ASR_UPSTREAM_WS=\K.*' .env | head -1 || echo "ws://127.0.0.1:10094")
  GATEWAY_PORT=$(grep -oP '^GATEWAY_PORT=\K.*' .env | head -1 || echo "8443")
else
  ASR_UPSTREAM="ws://127.0.0.1:10094"
  GATEWAY_PORT="8443"
fi

echo "ASR upstream: ${ASR_UPSTREAM}"
echo "Gateway listening on wss://0.0.0.0:${GATEWAY_PORT}"

existing_listener=""
if command -v ss >/dev/null 2>&1; then
  existing_listener=$(ss -lntp 2>/dev/null | awk -v port=":${GATEWAY_PORT}" '$4 ~ port "$" { print }')
fi

if [[ -n "${existing_listener}" ]]; then
  echo "[$(date '+%F %T')] port ${GATEWAY_PORT} is already in use:" >&2
  echo "${existing_listener}" >&2
  echo "Stop the existing process or set GATEWAY_PORT to another free port in .env." >&2
  exit 98
fi

RESTART_ON_KILL="${RESTART_ON_KILL:-1}"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-2}"
PYTHON_BIN="${PYTHON_BIN:-/home/twai/anaconda3/bin/python3.13}"

while true; do
  echo "[$(date '+%F %T')] starting gateway..."
  set +e
  "$PYTHON_BIN" https_gateway.py
  status=$?
  set -e

  if [[ ${status} -eq 0 ]]; then
    echo "[$(date '+%F %T')] gateway exited normally"
    exit 0
  fi

  echo "[$(date '+%F %T')] gateway exited with status ${status}" >&2
  if [[ ${status} -eq 137 ]]; then
    echo "[$(date '+%F %T')] status 137 means python received SIGKILL (often OOM killer, kill -9, or cgroup/system manager)" >&2
  fi

  if [[ "${RESTART_ON_KILL}" != "1" || ${status} -ne 137 ]]; then
    exit "${status}"
  fi

  echo "[$(date '+%F %T')] restarting gateway in ${RESTART_DELAY_SECONDS}s after SIGKILL..." >&2
  sleep "${RESTART_DELAY_SECONDS}"
done
