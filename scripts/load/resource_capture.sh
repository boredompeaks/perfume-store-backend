#!/usr/bin/env bash
# Resource capture for the load/stress runs: background samplers writing into
# a run directory, blocking until Ctrl-C (INT/TERM) or an optional duration.
#
#   usage: bash scripts/load/resource_capture.sh [outdir] [duration-seconds]
#   outdir defaults to load-runs/<UTC timestamp>; duration 0/empty = until
#   Ctrl-C.
#
# Samplers are enabled from the environment, never from this file: LOAD_PG_DSN
# (unset skips PG sampling), LOAD_PG_INTERVAL (10s), LOAD_DOCKER_INTERVAL (5s),
# LOAD_DOCKER_FILTER, and SENTRY_AUTH_TOKEN/SENTRY_ORG/SENTRY_PROJECT for a
# stop-time Sentry pull (LOAD_SENTRY_LOOKBACK_MIN: duration rounded up, min 15).
#
# This script SAMPLES ONLY: it never mutates the database, never restarts
# anything, and never prints the DSN or a token. A planned duration stop exits
# 0; a Ctrl-C exits 130.

set -uo pipefail

outdir="${1:-}"
if [ -z "$outdir" ]; then
  outdir="load-runs/$(date -u +%Y%m%dT%H%M%SZ)"
fi
duration="${2:-0}"

pg_interval="${LOAD_PG_INTERVAL:-10}"
docker_interval="${LOAD_DOCKER_INTERVAL:-5}"
sentry_base="${SENTRY_BASE_URL:-https://sentry.io}"

mkdir -p "$outdir" || { echo "FATAL: cannot create outdir '$outdir'" >&2; exit 2; }
outdir="$(cd "$outdir" && pwd)"

warnings="$outdir/sampler_warnings.txt"
manifest="$outdir/manifest.txt"
: >"$warnings"

warn() {
  # Sampler problems are data for the report, not script failures: record them
  # and keep the remaining samplers alive.
  echo "$(date -u +%FT%TZ) $*" >>"$warnings"
  echo "WARN: $*" >&2
}

pids=()
started_iso="$(date -u +%FT%TZ)"
started_epoch="$(date -u +%s)"
stopped=0

cleanup() {
  # INT/TERM and EXIT can both land; the guard writes the manifest exactly once.
  [ "$stopped" -eq 1 ] && return 0
  stopped=1
  echo "" >&2
  echo "stopping samplers..." >&2
  local pid
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null
  done
  local ended_epoch ended_iso
  ended_epoch="$(date -u +%s)"
  ended_iso="$(date -u +%FT%TZ)"
  {
    echo "task: SPEC-T2a resource capture"
    echo "started_utc: $started_iso"
    echo "ended_utc: $ended_iso"
    echo "duration_seconds: $((ended_epoch - started_epoch))"
    echo "outdir: $outdir"
    echo "docker_interval_s: $docker_interval"
    echo "pg_interval_s: $pg_interval"
    if [ -n "${LOAD_PG_DSN:-}" ] && command -v psql >/dev/null 2>&1; then
      echo "pg_sampling: enabled (DSN from env, value not recorded)"
    else
      echo "pg_sampling: skipped"
    fi
    echo "sentry_pull: $([ -f "$outdir/sentry_issues.json" ] && echo written || echo skipped)"
    echo "sampler_warnings: $warnings"
  } >"$manifest"

  # Sentry review is a STOP-time step: the whole capture window is on the
  # clock and a single pull beats a polling loop against the API.
  if [ -n "${SENTRY_AUTH_TOKEN:-}" ] && [ -n "${SENTRY_ORG:-}" ] && [ -n "${SENTRY_PROJECT:-}" ]; then
    lookback_min="${LOAD_SENTRY_LOOKBACK_MIN:-}"
    if [ -z "$lookback_min" ]; then
      lookback_min=$(( (ended_epoch - started_epoch) / 60 + 15 ))
      [ "$lookback_min" -lt 15 ] && lookback_min=15
    fi
    since_epoch=$((ended_epoch - lookback_min * 60))
    sentry_url="$sentry_base/api/0/organizations/$SENTRY_ORG/issues/?project=$SENTRY_PROJECT&query=&sort=date&since=$since_epoch&limit=100"
    if curl -fsS --max-time 30 \
      -H "Authorization: Bearer $SENTRY_AUTH_TOKEN" \
      "$sentry_url" >"$outdir/sentry_issues.json" 2>>"$warnings"; then
      echo "sentry_issues.json written (window ${lookback_min}m)" >&2
    else
      warn "sentry issues pull failed (see sampler_warnings.txt)"
    fi
  else
    echo "sentry pull: skipped (SENTRY_AUTH_TOKEN/SENTRY_ORG/SENTRY_PROJECT not all set)" >&2
  fi

  echo "capture complete: $outdir" >&2
}

# The duration auto-stop drops this sentinel before signalling, so on_signal
# can exit 0 for a planned stop and 130 for a Ctrl-C.
duration_stop_flag="$outdir/.duration_stop"

on_signal() {
  cleanup
  if [ -f "$duration_stop_flag" ]; then
    echo "duration completed — exiting 0" >&2
    exit 0
  fi
  exit 130
}

trap on_signal INT TERM
trap cleanup EXIT

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker_csv="$outdir/docker_stats.csv"
  docker_args="stats --no-stream --format {{.Name}},{{.ID}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}"
  # shellcheck disable=SC2086  # LOAD_DOCKER_FILTER is intentionally word-split
  [ -n "${LOAD_DOCKER_FILTER:-}" ] && docker_args="$docker_args --filter $LOAD_DOCKER_FILTER"
  (
    echo "sampled_at_utc,name,id,cpu_pct,mem_usage,mem_perc,net_io,block_io,pids" >"$docker_csv"
    while :; do
      # shellcheck disable=SC2086
      docker $docker_args 2>>"$warnings" | while IFS= read -r line; do
        echo "$(date -u +%FT%TZ),$line" >>"$docker_csv"
      done
      sleep "$docker_interval"
    done
  ) &
  pids+=($!)
  echo "sampler: docker stats -> docker_stats.csv (every ${docker_interval}s)" >&2
else
  warn "docker sampler skipped (docker CLI or daemon not reachable)"
fi

if [ -n "${LOAD_PG_DSN:-}" ]; then
  if command -v psql >/dev/null 2>&1; then
    (
      n=0
      mem_probe_done=0
      while :; do
        n=$((n + 1))
        # Full activity table: the connection/pool evidence the knee rubric reads.
        psql "$LOAD_PG_DSN" -X -A -F',' -c \
          "SELECT now() AS sampled_at, pid, usename, application_name, client_addr, state, wait_event_type, wait_event, NOW() - query_start AS query_age, LEFT(query, 200) AS query_head FROM pg_stat_activity" \
          >>"$outdir/pg_stat_activity_$n.csv" 2>>"$warnings" ||
          warn "pg_stat_activity snapshot $n failed"

        # Pool/worker ceilings: re-read each snapshot so an ALTER is caught.
        psql "$LOAD_PG_DSN" -X -c \
          "SELECT name, setting, unit, source, pending_restart FROM pg_settings WHERE name ~ '(^max_connections$|^shared_buffers$|work_mem|maint|effective_cache|max_worker|max_parallel|max_locks|max_prepared)' ORDER BY name" \
          >>"$outdir/pg_mem_settings_$n.txt" 2>>"$warnings" ||
          warn "pg_mem_settings snapshot $n failed"

        # pg_stat_memsettings needs PostgreSQL 17+; probe once, then stop
        # retrying (with a warning) on servers that lack the relation.
        if [ "$mem_probe_done" -eq 0 ]; then
          if psql "$LOAD_PG_DSN" -X -c "SELECT * FROM pg_stat_memsettings" \
            >>"$outdir/pg_stat_memsettings_$n.txt" 2>>"$warnings"; then
            mem_probe_done=2
          else
            warn "pg_stat_memsettings unavailable on this server (needs PostgreSQL 17+); not retried"
            mem_probe_done=1
          fi
        elif [ "$mem_probe_done" -eq 2 ]; then
          psql "$LOAD_PG_DSN" -X -c "SELECT * FROM pg_stat_memsettings" \
            >>"$outdir/pg_stat_memsettings_$n.txt" 2>>"$warnings" ||
            warn "pg_stat_memsettings snapshot $n failed"
        fi

        sleep "$pg_interval"
      done
    ) &
    pids+=($!)
    echo "sampler: pg_stat_activity + pg_mem_settings (every ${pg_interval}s, DSN from env)" >&2
  else
    warn "pg samplers skipped (psql not on PATH; run capture on the box or install client)"
  fi
else
  warn "pg samplers skipped (LOAD_PG_DSN unset — never typed into this file)"
fi

if [ "$duration" -gt 0 ] 2>/dev/null; then
  (
    sleep "$duration"
    echo "duration ${duration}s elapsed — stopping" >&2
    # Sentinel FIRST: on_signal tells this planned stop (0) from a Ctrl-C (130).
    touch "$duration_stop_flag"
    kill -TERM $$ 2>/dev/null
  ) &
  pids+=($!)
  echo "auto-stop after ${duration}s (Ctrl-C works before then)" >&2
else
  echo "running until Ctrl-C" >&2
fi

echo "capture dir: $outdir" >&2
wait -n 2>/dev/null || wait
