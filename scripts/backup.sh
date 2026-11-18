#!/usr/bin/env bash
# Scheduled database backup: one entry point for cron, the rehearsed backup
# and a manual run. Non-zero exit means no dump was kept (a refused run, a
# missing pg_dump or a failed dump); cron mails that.
#
# BACKUP_DIR is REQUIRED and absolute: the dumps are bind-mounted out of a
# one-shot container, so a relative path or an unmounted `compose run` writes
# a backup that dies with the container. BACKUP_RETENTION defaults to 7 so
# `set -u` cannot abort the cadence before the first dump.
#
# PREREQUISITE: manage.py backup_db shells out to pg_dump, which the backend
# image does not ship; backend/docs/deploy-runbook.md ("Backups") has the
# workaround.

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker-compose.yml
SERVICE=backend
# Fixed in-container mount path, so the host and container views of the
# backup directory cannot drift apart.
CONTAINER_BACKUP_DIR=/backups

log() { printf '\n=== backup: %s\n' "$*"; }
fail() { printf 'backup: %s\n' "$*" >&2; exit 1; }

[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found (run this from the repository)."
[ -f .env ] || fail \
  'no .env at the repository root - it carries the deploy secrets (see the env contract at the top of docker-compose.yml). It is git-ignored and must never be committed.'

BACKUP_DIR=${BACKUP_DIR:-}
BACKUP_RETENTION=${BACKUP_RETENTION:-7}
[ -n "$BACKUP_DIR" ] || fail \
  'BACKUP_DIR is not set. Name an absolute HOST directory for the dumps, e.g. BACKUP_DIR=/srv/perfume-store/backups (see backend/docs/deploy-runbook.md, "Backups").'
case "$BACKUP_DIR" in
/*) ;;
*) fail "BACKUP_DIR must be an absolute host path (got '$BACKUP_DIR'); a relative path depends on the working directory of whatever runs the scheduler." ;;
esac

mkdir -p "$BACKUP_DIR"
[ -d "$BACKUP_DIR" ] || fail "BACKUP_DIR '$BACKUP_DIR' is not a directory."

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# BACKUP_DIR is overridden with the container-side path, and BACKUP_RETENTION
# is passed explicitly: `docker compose run` does not forward the host
# environment.
manage() {
  compose run --rm -T \
    -v "$BACKUP_DIR:$CONTAINER_BACKUP_DIR" \
    -e BACKUP_DIR="$CONTAINER_BACKUP_DIR" \
    -e BACKUP_RETENTION="$BACKUP_RETENTION" \
    "$SERVICE" python manage.py "$@"
}

log "dump and prune into $BACKUP_DIR (keeping $BACKUP_RETENTION dumps, default 7)"
manage backup_db