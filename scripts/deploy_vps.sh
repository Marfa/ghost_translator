#!/usr/bin/env bash
# Deploy / update ghost-translator on the VPS from the local git checkout.
# Expected layout: /opt/ghost-translator with compose.vps.yml and .env
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ghost-translator}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8082/health}"

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: $APP_DIR/.env is missing" >&2
  exit 1
fi

# Keep secrets across hard reset
cp -a .env /tmp/ghost-translator.env.bak

git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

cp -a /tmp/ghost-translator.env.bak .env
rm -f /tmp/ghost-translator.env.bak

docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans

for _ in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "health: ok"
    docker compose -f "$COMPOSE_FILE" ps
    exit 0
  fi
  sleep 2
done

echo "WARNING: /health did not become ready in time" >&2
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --tail=80 || true
exit 1
