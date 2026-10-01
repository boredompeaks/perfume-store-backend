#!/usr/bin/env bash
# SPEC-22-07: the release sequence for the deployable unit.
#
# ONE script, ONE release-time migrate path: `manage.py migrate --noinput`
# below is the only place a release migrates. There is deliberately no
# `release:` line in the Procfile and no migrate step in docker-compose.yml -
# a second path is a second thing to keep correct, and they always disagree.
# CI: .github/workflows/deploy.yml runs this exact script, only after the
# existing backend-tests and secret-scan workflows have passed on that commit.
#
# Sequence (every step is idempotent, so a re-run is a no-op):
#   1. pull     build the released image, refresh the database image
#   2. database start it and wait until it accepts connections
#   3. review   migration-review checkpoint (R-22.11) - refuses to release
#               when `makemigrations --check` would write a migration
#   4. migrate  manage.py migrate --noinput
#   5. assets   manage.py collectstatic --noinput
#   6. restart  recreate the app on the released image, wait for /health/
#
# Migration review (R-22.11): step 3 IS the checkpoint, and it is fail-closed.
# A model change must arrive with its migration file already committed, so the
# schema change is reviewed in the same change as the model that caused it. A
# release may never generate - let alone silently apply - a migration nobody
# looked at. See backend/docs/changes.md (SPEC-22-07) for the rationale.
#
# Requirements: bash 4+, Docker Compose v2 (`docker compose`), and a repo-root
# .env holding the deploy secrets (git-ignored; this script never prints it).
# Run it from the deploy host with the repository checked out at the released
# commit:
#   ./scripts/release.sh
# It is also the local release path (`./scripts/release.sh` after
# `docker compose build`), so a rehearsed release and a real one are the same
# commands.
#
# Knobs (all optional):
#   RELEASE_GIT_PULL=1  also `git pull --ff-only` the checkout first. Off by
#                       default: on a deploy host the workflow has already
#                       checked out the exact released commit.
#   DB_WAIT_SECONDS     how long to wait for the database (default 120)
#   APP_WAIT_SECONDS    how long to wait for /health/ after the restart
#                       (default 180)

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker-compose.yml
SERVICE=backend
DB_SERVICE=db
DB_WAIT_SECONDS=${DB_WAIT_SECONDS:-120}
APP_WAIT_SECONDS=${APP_WAIT_SECONDS:-180}

log() { printf '\n=== release: %s\n' "$*"; }
fail() { printf 'release: %s\n' "$*" >&2; exit 1; }

# Fail before touching anything if the environment is not wired: compose reads
# the deploy secrets from .env, and every var it interpolates would otherwise
# be reported one confusing line at a time.
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found (run this from the repository)."
[ -f .env ] || fail \
  'no .env at the repository root - it carries the deploy secrets (see the env contract at the top of docker-compose.yml). It is git-ignored and must never be committed.'

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# One-shot management command on the released image, in the project network, so
# it always talks to the same code and the same database the app will serve.
manage() { compose run --rm -T "$SERVICE" python manage.py "$@"; }

# 1. Pull ---------------------------------------------------------------
if [ "${RELEASE_GIT_PULL:-0}" = "1" ]; then
  log 'pull: fast-forward the checkout'
  git pull --ff-only
fi

log 'pull: build the released image and refresh the database image'
compose build --pull "$SERVICE"
compose pull --quiet "$DB_SERVICE"

# 2. Database -----------------------------------------------------------
# Migrations run against the live database, so it has to be up and accepting
# connections first - `--wait` blocks on the service healthcheck
# (pg_isready) instead of racing it.
log 'database: start and wait until it accepts connections'
compose up -d --wait --wait-timeout "$DB_WAIT_SECONDS" "$DB_SERVICE"

# 3. Migration-review checkpoint (R-22.11) ------------------------------
# Exit code is the signal: makemigrations --check exits non-zero when a model
# change has no committed migration, and we say so in words rather than letting
# a bare status code be the only evidence.
log 'review: refuse to release when model changes are missing their migration'
if ! manage makemigrations --check --dry-run; then
  fail 'model changes without committed migrations: run `manage.py makemigrations`, commit the generated files under backend/*/migrations/, and re-run. R-22.11 requires migrations to be reviewed and present before release.'
fi

# 4. Migrate (the only release-time migrate) ---------------------------
log 'migrate: apply migrations'
manage migrate --noinput

# 5. Assets -------------------------------------------------------------
# The image already collected at build time; this only refreshes what the
# released code changed, and whitenoise serves the result from STATIC_ROOT.
log 'assets: collectstatic'
manage collectstatic --noinput

# 6. Restart ------------------------------------------------------------
# Recreating the container is the gunicorn restart: the image CMD is
# `exec gunicorn ...`, so PID 1 IS gunicorn and the recreate drains its
# workers with a real signal instead of killing them behind a supervisor's
# back. `--wait` blocks until /health/ answers 200 (it answers 503 while the
# database or media mount is degraded), so the release is only reported as
# finished once the app actually serves traffic.
log 'restart: run the app on the released image and wait for /health/'
compose up -d --wait --wait-timeout "$APP_WAIT_SECONDS" --remove-orphans "$SERVICE"
compose ps