#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "release-uat: repo-root .env is required" >&2
  exit 1
fi
# Ensure external services
docker start perfume-pg perfume-redis 2>/dev/null || true
docker network inspect perfume-net >/dev/null 2>&1 || docker network create perfume-net
# Join pg/redis to perfume-net if not already
docker network connect perfume-net perfume-pg 2>/dev/null || true
docker network connect perfume-net perfume-redis 2>/dev/null || true

COMPOSE="docker compose -f docker-compose.yml -f compose.uat.yml"
echo "==> build"
$COMPOSE build --pull backend
echo "==> migrate"
$COMPOSE run --rm --no-deps --network perfume-net backend python manage.py migrate --noinput
echo "==> collectstatic"
$COMPOSE run --rm --no-deps --network perfume-net backend python manage.py collectstatic --noinput
echo "==> up"
$COMPOSE up -d --no-deps --wait --wait-timeout 180 backend
echo "==> health"
curl -fsS -H "Host: api.boredompeaks.me" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ | head -c 200
echo
echo "release-uat OK"