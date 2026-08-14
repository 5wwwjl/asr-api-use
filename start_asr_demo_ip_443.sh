#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="asr-demo-ip-443"
IMAGE="${ASR_DEMO_PROXY_IMAGE:-nginx:latest}"
CONFIG="$BASE_DIR/docker/asr-demo-ip-443.conf"
PROXY_PARAMS="$BASE_DIR/docker/asr-demo-ip-proxy-params.conf"
CERT="$BASE_DIR/certs/asr-ip-192.168.173.167.pem"
KEY="$BASE_DIR/certs/asr-ip-192.168.173.167.key"

for path in "$CONFIG" "$PROXY_PARAMS" "$CERT" "$KEY"; do
    if [[ ! -f "$path" ]]; then
        echo "ERROR: required file is missing: $path" >&2
        exit 1
    fi
done

docker run --rm --network host \
    -v "$CONFIG:/etc/nginx/nginx.conf:ro" \
    -v "$PROXY_PARAMS:/etc/nginx/proxy_params.conf:ro" \
    -v "$CERT:/etc/nginx/tls/asr-ip.pem:ro" \
    -v "$KEY:/etc/nginx/tls/asr-ip.key:ro" \
    "$IMAGE" nginx -t

if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker rm -f "$CONTAINER" >/dev/null
fi

docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --network host \
    -v "$CONFIG:/etc/nginx/nginx.conf:ro" \
    -v "$PROXY_PARAMS:/etc/nginx/proxy_params.conf:ro" \
    -v "$CERT:/etc/nginx/tls/asr-ip.pem:ro" \
    -v "$KEY:/etc/nginx/tls/asr-ip.key:ro" \
    "$IMAGE" >/dev/null

for attempt in $(seq 1 30); do
    if curl -kfsS --connect-timeout 2 --max-time 4 \
        https://192.168.173.167/accuracy_ae_demo.html >/dev/null 2>&1; then
        echo "ASR A/E demo ready: https://192.168.173.167/"
        exit 0
    fi
    sleep 0.5
done

echo "ERROR: ASR demo proxy did not become ready" >&2
docker logs --tail 50 "$CONTAINER" >&2 || true
exit 1
