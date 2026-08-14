#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Keep 8443 exclusively for the existing ASR gateway. The executive demo uses
# the real 167 location REST/read-only database but does not consume the MQ
# queue itself; 8443 remains the sole real address-scope consumer.
DEMO_PORT="${HOTWORD_DEMO_PORT:-18448}"
PYTHON_BIN="${PYTHON_BIN:-/home/twai/anaconda3/bin/python3.13}"

if command -v ss >/dev/null 2>&1 && \
   ss -lnt 2>/dev/null | awk -v port=":${DEMO_PORT}" '$4 ~ port "$" { found=1 } END { exit !found }'; then
  echo "port ${DEMO_PORT} is already in use" >&2
  exit 98
fi

export GATEWAY_HOST="0.0.0.0"
export GATEWAY_PORT="${DEMO_PORT}"
export ASR_ADDRESS_SCOPE_MQ_ENABLED="false"
export ASR_ADDRESS_SCOPE_CREDENTIAL_ENV_FILE="${ASR_ADDRESS_SCOPE_CREDENTIAL_ENV_FILE:-/home/twai/wjx/location-real/.env}"
export ASR_ADDRESS_SCOPE_SOURCE="database"
export ASR_ADDRESS_SCOPE_DB_HOST="192.168.173.167"
export ASR_ADDRESS_SCOPE_DB_PORT="15433"
export ASR_ADDRESS_SCOPE_DB_NAME="dispatch_assist"
export ASR_ADDRESS_SCOPE_BASE_URL="http://192.168.173.167:18082"
export HOTWORD_DEMO_LOCATION_BASE_URL="http://192.168.173.167:18082"
export HOTWORD_DEMO_LOCATION_LONGITUDE="113.93924230"
export HOTWORD_DEMO_LOCATION_LATITUDE="22.55250952"
export HOTWORD_DEMO_LOCATION_RADIUS_METERS="2000"
export HOTWORD_DEMO_LOCATION_ACCURACY_METERS="30"
export HOTWORD_DEMO_LOCATION_INVENTORY_VERSION="ODS7ALM_AI_REAL_20260810_V1"

echo "Hotword demo: https://0.0.0.0:${DEMO_PORT}/hotword_demo.html"
exec "${PYTHON_BIN}" https_gateway.py
