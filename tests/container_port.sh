#!/usr/bin/env bash
# Smoke-test the real image with a non-default runtime port; no GPU required.
set -euo pipefail
container_name="asr-port-test-${RANDOM}"
trap 'docker rm -f "$container_name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$container_name" --read-only \
  --tmpfs /tmp --tmpfs /data:uid=10001,gid=10001 \
  -e PORT=8282 \
  -e ASR_API_KEY=synthetic-container-test-api-key \
  -e ASR_MODEL=test-model \
  -e ASR_BACKEND_URL=http://backend.invalid/v1 \
  -e ASR_HEALTH_URL=http://backend.invalid/health \
  asr-gateway:test >/dev/null
for attempt in {1..20}; do
  if docker exec "$container_name" python -c \
    'import json, os, urllib.request; assert os.environ["PORT"] == "8282"; assert json.load(urllib.request.urlopen("http://127.0.0.1:" + os.environ["PORT"] + "/health", timeout=2))["status"] == "ok"' \
      >/dev/null 2>&1; then
    printf 'PASS: container serves health on runtime PORT=8282\n'
    exit 0
  fi
  sleep 1
done
docker logs "$container_name"
exit 1
