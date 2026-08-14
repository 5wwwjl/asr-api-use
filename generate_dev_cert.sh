#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
CERT_FILE="${TLS_CERT_FILE:-cert.pem}"
KEY_FILE="${TLS_KEY_FILE:-key.pem}"
CN="${TLS_CN:-localhost}"
DNS_NAMES="${TLS_DNS_NAMES:-localhost,$(hostname -f 2>/dev/null || hostname)}"
IP_NAMES="${TLS_IP_NAMES:-127.0.0.1,$(hostname -I 2>/dev/null | tr ' ' ',' | sed 's/,$//')}"

SAN_ENTRIES=()
IFS=',' read -ra DNS_PARTS <<< "${DNS_NAMES}"
for name in "${DNS_PARTS[@]}"; do
  [[ -n "${name}" ]] && SAN_ENTRIES+=("DNS:${name}")
done

IFS=',' read -ra IP_PARTS <<< "${IP_NAMES}"
for ip in "${IP_PARTS[@]}"; do
  [[ -n "${ip}" ]] && SAN_ENTRIES+=("IP:${ip}")
done

SAN="$(IFS=,; echo "${SAN_ENTRIES[*]}")"

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout "${KEY_FILE}" \
  -out "${CERT_FILE}" \
  -days 3650 \
  -subj "/CN=${CN}" \
  -addext "subjectAltName=${SAN}"

echo "generated ${CERT_FILE} and ${KEY_FILE}"
