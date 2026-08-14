#!/usr/bin/env bash
set -euo pipefail

if (( $# > 0 )); then
  exec "$@"
fi

if [[ ! -f "${FUNASR_MODEL_DIR}/model.pt" ]]; then
  echo "ERROR: Paraformer model not found: ${FUNASR_MODEL_DIR}/model.pt" >&2
  echo "Set FUNASR_MODEL_DIR in the container and mount the model directory there." >&2
  exit 2
fi

exec python /workspace/funasr_server_xhw.py \
  --host "${FUNASR_HOST}" \
  --port "${FUNASR_PORT}" \
  --model_dir "${FUNASR_MODEL_DIR}" \
  --vad_model "${FUNASR_VAD_MODEL}" \
  --vad_kwargs "${FUNASR_VAD_KWARGS}" \
  --ngpu "${FUNASR_NGPU}" \
  --ncpu "${FUNASR_NCPU}" \
  --disable_update

