#!/usr/bin/env bash
# SPEC-22-07: the release sequence for the deployable unit.
#
# ONE script, ONE release-time migrate path: `manage.py migrate --noinput`
# below is the only place a release migrates. There is deliberately no
# `release:` line in the Procfile and no migrate step in docker-compose.yml -
# a second path is a second thing to keep correct, and they always disagree.
# CI: .github/workflows/deploy.yml reaches its release through one ssh call
# that runs the repo's release script on the deploy host - and since
# scripts/release-uat.sh is tracked executable, deploy.yml's `-x` test selects
# THAT script (the UAT flow over the perfume-net network); this script is
# what runs when it is absent, and what an operator runs by hand. Either way
# the call happens only after the existing backend-tests and secret-scan
# workflows have passed on that commit.
#
# Sequence (every step is idempotent, so a re-run is a no-op):
#   1. pull     build the released image, refresh the database and redis images
#   2. substrate start db and redis, wait until both accept connections
#   3. review   migration-review checkpoint (R-22.11) - refuses to release
#               when `makemigrations --check` would write a migration
#   4. migrate  manage.py migrate --noinput
#   5. assets   manage.py collectstatic --noinput
#   6. restart  recreate the app on the released image, wait for /health/,
#               then recreate the celery worker/beat on that same image
#
# The async substrate (redis + worker + beat in docker-compose.yml) is part
# of a release, not a side effect of one. Step 2 starts the broker before
# anything may need it; step 6 recreates worker and beat right after the
# app's /health/ gate, and compose replaces any container whose image id
# changed - so the beat-scheduled sweeps (expire_reservations, the outbox
# drain and purge) run on the released code the moment the release finishes.
# A release that only rebuilt the web container would leave every sweep on
# the previous release's image, quietly, with no failing step anywhere to
# say so.
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
#   DB_WAIT_SECONDS     how long to wait for the database and redis (default
#                       120)
#   APP_WAIT_SECONDS    how long to wait for /health/ after the restart
#                       (default 180)

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker-compose.yml
SERVICE=backend
DB_SERVICE=db
REDIS_SERVICE=redis
# The Celery processes that must be running the released image when a
# release ends. An array so each name survives `set -u` and word-splitting
# stays deliberate.
ASYNC_SERVICES=(worker beat)
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

log 'pull: build the released image and refresh the database and redis images'
compose build --pull "$SERVICE"
compose pull --quiet "$DB_SERVICE"
compose pull --quiet "$REDIS_SERVICE"

# 2. Database and broker --------------------------------------------------
# Migrations run against the live database, so it has to be up and accepting
# connections first - `--wait` blocks on the service healthcheck (pg_isready)
# instead of racing it. Redis comes up in the same step because step 6
# starts worker and beat, whose broker it is; `--wait` blocks on its
# healthcheck (redis-cli ping) for the same reason, so the async processes
# are never created against a broker that has not answered yet.
log 'database and broker: start and wait until both accept connections'
compose up -d --wait --wait-timeout "$DB_WAIT_SECONDS" "$DB_SERVICE" "$REDIS_SERVICE"

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
#
# The worker and the beat scheduler are recreated in their own `up` right
# after: compose replaces any container whose image id changed, so both land
# on the image built in step 1 and the beat-scheduled sweeps execute released
# code rather than the previous release's. They are NOT in the `--wait` call
# above, and that is measured rather than assumed: Docker Compose refuses
# `--wait` for a service with no healthcheck ("container ... has no
# healthcheck configured", exit 1), and the inherited image healthcheck is
# disabled for them in docker-compose.yml because it probes gunicorn's
# /health/, which a Celery container does not serve - it marks a healthy
# worker unhealthy, which is worse than having none. A real replacement does
# not exist for beat (`inspect ping` answers workers, not the scheduler), so
# the release gates on the ONE honest signal - the app's /health/ - and
# `compose ps` below prints the async processes' state for the operator.
log 'restart: recreate worker and beat on the released image'
compose up -d "${ASYNC_SERVICES[@]}"
compose ps