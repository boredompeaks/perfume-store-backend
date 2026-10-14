#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "release-uat: repo-root .env is required" >&2
  exit 1
fi
set -a; . ./.env; set +a
docker start perfume-pg perfume-redis 2>/dev/null || true
docker network inspect perfume-net >/dev/null 2>&1 || docker network create perfume-net
docker network connect perfume-net perfume-pg 2>/dev/null || true
docker network connect perfume-net perfume-redis 2>/dev/null || true

COMPOSE="docker compose -f docker-compose.yml -f compose.uat.yml"
echo "==> build"
$COMPOSE build --pull backend

run_manage() {
  docker run --rm --network perfume-net \
    --env-file .env \
    -e DATABASE_URL \
    -e REDIS_URL \
    -e DJANGO_SECRET_KEY \
    -e DJANGO_ENV \
    -e DJANGO_ALLOWED_HOSTS \
    -e CSRF_TRUSTED_ORIGINS \
    perfume-backend:latest \
    python manage.py "$@"
}

echo "==> migrate"
run_manage migrate --noinput
echo "==> collectstatic"
run_manage collectstatic --noinput
echo "==> up"
$COMPOSE up -d --no-deps --wait --wait-timeout 180 backend
echo "==> health"
curl -fsS -H "Host: api.boredompeaks.me" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ | head -c 250
echo
echo "release-uat OK"