#!/usr/bin/env bash
# UAT release: same gates as release.sh but uses compose.uat.yml (external PG/Redis).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "release-uat: repo-root .env is required" >&2
  exit 1
fi
COMPOSE="docker compose -f docker-compose.yml -f compose.uat.yml"
echo "==> build"
$COMPOSE build --pull backend
echo "==> migrate"
$COMPOSE run --rm --no-deps backend python manage.py migrate --noinput
echo "==> collectstatic"
$COMPOSE run --rm --no-deps backend python manage.py collectstatic --noinput
echo "==> up"
$COMPOSE up -d --no-deps --wait --wait-timeout 180 backend
echo "==> health"
curl -fsS -H "Host: api.boredompeaks.me" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ | head -c 200
echo
echo "release-uat OK"
