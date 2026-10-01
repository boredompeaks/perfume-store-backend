#!/usr/bin/env bash
# SPEC-22-02 [R-22.9]: the scheduled database backup.
#
# ONE entry point for the backup cadence, so the cron line, the rehearsed
# backup and an operator's manual run are the same command:
#
#   BACKUP_DIR=/srv/perfume-store/backups ./scripts/backup.sh
#
# Install it as a cron entry on the deploy host (backend/docs/deploy-runbook.md,
# "Backups" carries the exact line and the systemd-timer alternative):
#
#   30 2 * * * cd /srv/perfume-store && BACKUP_DIR=/srv/perfume-store/backups \
#         ./scripts/backup.sh >> /var/log/perfume-store-backup.log 2>&1
#
# What it does, and why each step is the way it is:
#
#   1. check   fail before touching anything if the environment is not wired.
#              BACKUP_DIR is REQUIRED and must be an absolute HOST path: the
#              dumps have to outlive the container, so the directory is
#              bind-mounted into the one-shot container below. A relative path
#              or a bare `docker compose run` with no mount writes the dump
#              into a container filesystem that is thrown away when the
#              container is replaced - a backup that survives nothing.
#   2. dump    `manage.py backup_db` on the released image, in the project
#              network, so what is dumped is what ships and it talks to the
#              same database the app serves. The command reads DATABASE_URL
#              out of the container's own environment (compose assembles it
#              from the same POSTGRES_* variables the db service reads) and
#              never prints it.
#   3. prune   the same command applies the retention policy (BACKUP_RETENTION,
#              default 7) in the same run: keep the newest N dumps, delete the
#              rest, exit 0.
#
# Exit status is the signal: non-zero means no dump was kept (a refused run, a
# missing pg_dump, a failed dump). Cron mails that; nothing here decides
# whether a backup happened.
#
# PREREQUISITE. `manage.py backup_db` shells out to `pg_dump`, which the
# backend image does not ship (SPEC-2-10a installs psycopg, the driver, not
# the client). Until the image carries the PostgreSQL client this script
# stops with that refusal by name; backend/docs/deploy-runbook.md ("Backups")
# gives the one-time image step and the no-image-change alternative (dump with
# the postgres image's own pg_dump, then prune with `--prune-only`).
#
# Requirements: bash 4+, Docker Compose v2 (`docker compose`), a repo-root
# .env holding the deploy secrets (git-ignored; this script never prints it),
# and a backup directory the app's uid (10001, see backend/Dockerfile) can
# write to.
#
# Knobs (all env-driven):
#   BACKUP_DIR      REQUIRED. Absolute host path the dumps are written to.
#   BACKUP_RETENTION how many dumps to keep, newest first (default 7).

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker-compose.yml
SERVICE=backend
# Where the host backup directory is mounted inside the one-shot container.
# A fixed in-container path, so the host and container views of "the backup
# directory" cannot drift apart.
CONTAINER_BACKUP_DIR=/backups

log() { printf '\n=== backup: %s\n' "$*"; }
fail() { printf 'backup: %s\n' "$*" >&2; exit 1; }

# 1. Check ---------------------------------------------------------------
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found (run this from the repository)."
[ -f .env ] || fail \
  'no .env at the repository root - it carries the deploy secrets (see the env contract at the top of docker-compose.yml). It is git-ignored and must never be committed.'

BACKUP_DIR=${BACKUP_DIR:-}
[ -n "$BACKUP_DIR" ] || fail \
  'BACKUP_DIR is not set. Name an absolute HOST directory for the dumps, e.g. BACKUP_DIR=/srv/perfume-store/backups (see backend/docs/deploy-runbook.md, "Backups").'
case "$BACKUP_DIR" in
/*) ;;
*) fail "BACKUP_DIR must be an absolute host path (got '$BACKUP_DIR'); a relative path depends on the working directory of whatever runs the scheduler." ;;
esac

mkdir -p "$BACKUP_DIR"
[ -d "$BACKUP_DIR" ] || fail "BACKUP_DIR '$BACKUP_DIR' is not a directory."

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# One-shot management command on the released image, in the project network,
# with the backup directory bind-mounted so the dump outlives the container.
# BACKUP_DIR is overridden with the container-side path: the key in .env names
# a HOST path, and passing the host value through unchanged would write into
# the container's own filesystem.
manage() {
  compose run --rm -T \
    -v "$BACKUP_DIR:$CONTAINER_BACKUP_DIR" \
    -e BACKUP_DIR="$CONTAINER_BACKUP_DIR" \
    "$SERVICE" python manage.py "$@"
}

# 2. Dump + 3. Prune ------------------------------------------------------
# One command does both: it takes the dump and then applies the retention
# policy. A non-zero exit (a refused run, a missing pg_dump, a failed dump)
# aborts the script, so the schedule never reports success without a backup.
log "dump and prune into $BACKUP_DIR (keeping $BACKUP_RETENTION dumps, default 7)"
manage backup_db