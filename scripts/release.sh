#!/usr/bin/env bash
# Release sequence for the deployable unit: pull, start db+redis, migration
# review, migrate, collectstatic, restart. Every step is idempotent.
#
# `manage.py migrate` below is the ONLY release-time migrate path (no Procfile
# release line, no compose migrate step): a second path always drifts.
#
# Step 3 is fail-closed: `makemigrations --check` exits non-zero when a model
# change has no committed migration, so a release can never generate one.
#
# Worker and beat are recreated after the app because compose replaces any
# container whose image id changed. They are not in the `--wait` call because
# compose refuses `--wait` for a service with no healthcheck, and the inherited
# image healthcheck probes /health/, which a Celery container does not serve.
#
# deploy.yml runs scripts/release-uat.sh when it is executable (it is tracked
# that way); this script is the fallback and the manual release path.
#
# Knobs: RELEASE_GIT_PULL=1 also `git pull --ff-only` (off by default: the
# deploy host is already at the released commit); DB_WAIT_SECONDS (120) and
# APP_WAIT_SECONDS (180) bound the waits.

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker-compose.yml
SERVICE=backend
DB_SERVICE=db
REDIS_SERVICE=redis
# The Celery processes that must run the released image when a release ends.
# An array so each name survives `set -u` and word-splitting stays deliberate.
ASYNC_SERVICES=(worker beat)
DB_WAIT_SECONDS=${DB_WAIT_SECONDS:-120}
APP_WAIT_SECONDS=${APP_WAIT_SECONDS:-180}

log() { printf '\n=== release: %s\n' "$*"; }
fail() { printf 'release: %s\n' "$*" >&2; exit 1; }

# Fail before touching anything: compose reads the deploy secrets from .env
# and would otherwise report each missing variable one line at a time.
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found (run this from the repository)."
[ -f .env ] || fail \
  'no .env at the repository root - it carries the deploy secrets (see the env contract at the top of docker-compose.yml). It is git-ignored and must never be committed.'

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# One-shot management command on the released image, in the project network,
# so it talks to the same code and the same database the app serves.
manage() { compose run --rm -T "$SERVICE" python manage.py "$@"; }

if [ "${RELEASE_GIT_PULL:-0}" = "1" ]; then
  log 'pull: fast-forward the checkout'
  git pull --ff-only
fi

log 'pull: build the released image and refresh the database and redis images'
compose build --pull "$SERVICE"
compose pull --quiet "$DB_SERVICE"
compose pull --quiet "$REDIS_SERVICE"

# Migrations run against the live database, so `--wait` blocks on the db
# healthcheck instead of racing it; redis comes up in the same step because
# step 6 starts worker and beat against it.
log 'database and broker: start and wait until both accept connections'
compose up -d --wait --wait-timeout "$DB_WAIT_SECONDS" "$DB_SERVICE" "$REDIS_SERVICE"

log 'review: refuse to release when model changes are missing their migration'
if ! manage makemigrations --check --dry-run; then
  fail 'model changes without committed migrations: run `manage.py makemigrations`, commit the generated files under backend/*/migrations/, and re-run. R-22.11 requires migrations to be reviewed and present before release.'
fi

log 'migrate: apply migrations'
manage migrate --noinput

# The image already collected at build time; this refreshes what the released
# code changed, and whitenoise serves STATIC_ROOT.
log 'assets: collectstatic'
manage collectstatic --noinput

# Recreating the container IS the gunicorn restart (the image CMD is
# `exec gunicorn ...`, so PID 1 is gunicorn); `--wait` blocks until /health/
# answers 200, so the release is only reported finished once the app serves.
log 'restart: run the app on the released image and wait for /health/'
compose up -d --wait --wait-timeout "$APP_WAIT_SECONDS" --remove-orphans "$SERVICE"

# Worker and beat are recreated in their own `up` right after, so both land
# on the image built in step 1. They are deliberately absent from the `--wait`
# call above: the inherited healthcheck probes gunicorn's /health/, which a
# Celery container does not serve, and compose refuses `--wait` for a service
# with no healthcheck. A real replacement probe does not exist for beat, so
# the release gates on the app's /health/ and `compose ps` shows the rest.
log 'restart: recreate worker and beat on the released image'
compose up -d "${ASYNC_SERVICES[@]}"
compose ps