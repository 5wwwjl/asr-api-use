#!/usr/bin/env bash
set -euo pipefail

mkdir -p \
  "$(dirname "${TLS_CERT_FILE}")" \
  "$(dirname "${TLS_KEY_FILE}")" \
  "${ASR_RECORDINGS_DIR}" \
  "${ASR_BUSINESS_LOG_DIR}"

if [[ ! -s "${TLS_CERT_FILE}" || ! -s "${TLS_KEY_FILE}" ]]; then
  echo "[gateway] TLS certificate not found; generating a development certificate"
  /app/generate_dev_cert.sh
fi

if (( $# > 0 )); then
  exec "$@"
fi

exec python /app/https_gateway.py

