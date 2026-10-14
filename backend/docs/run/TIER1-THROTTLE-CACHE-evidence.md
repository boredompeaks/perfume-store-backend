# TIER1-THROTTLE-CACHE — evidence

Builder, lane B (`lane/b` @ `3b2c2f0`), 2026-10-14. Raw full-suite logs for
this task were kept on disk uncommitted (`TIER1-THROTTLE-CACHE-evidence-full.log`,
`TIER1-THROTTLE-CACHE-pre-redis.txt`, both ~1 MB) — this file is the compact,
committable summary. Every figure below was produced by a command run in this
session; none is carried forward from a handoff.

## Environment

- PostgreSQL: lane B container `pg2b` (port 5433, db `perfume_store_b`),
  engine confirmed by probe: `ENGINE postgresql | VERSION PostgreSQL 17.11 on
  x86_64-pc-linux-musl` (docker inspect reports `PG_VERSION=17.11`).
- Redis: container `redis-throttle-b` (`redis:7-alpine`, host port 6389),
  reached as `redis://127.0.0.1:6389/14` (dedicated DB index). `REDIS_URL`
  was passed as a process env var for the run commands only; no credential or
  URL is committed anywhere.

## Pre-flight (base `3b2c2f0`, before any change, no REDIS_URL)

`DATABASE_URL=postgres://…5433/perfume_store_b venv\Scripts\python -m
coverage run manage.py test --verbosity 0` + `coverage report`:
Ran 2058 tests, OK (expected failures=3); TOTAL 10165 stmts, 5 missed,
99.77%. Matches the recorded floor exactly.

## New-test fail-fast proof

Before the settings change, `manage.py test` pointed at the new test module
(`backend/tests/test_throttle_shared_cache.py`):
8 found, FAILED (failures=1, skipped=2) — the failure is
`ThrottleCacheConfigTests.test_with_redis_url_the_default_alias_resolves_to_redis`
(observed LocMemCache where the spec requires RedisCache); the 2 skips are the
live-Redis pins, not yet armed. After the change, bare run: OK (skipped=2).
Armed run (`REDIS_URL=redis://127.0.0.1:6389/14`): Ran 8 tests, OK, 0 skips —
including `test_two_workers_share_one_budget_not_one_budget_each` and
`test_count_is_shared_with_a_separately_spawned_process`.

## Authoritative floor (GATE-7 printed)

`DATABASE_URL=postgres://…5433/perfume_store_b REDIS_URL=redis://127.0.0.1:6389/14
venv\Scripts\python ..\scripts\changelog_figures.py floor --task
TIER1-THROTTLE-CACHE` — both legs inherited REDIS_URL, so the live-Redis pins
ran on both engines (zero skips):

- sqlite: Ran 2067 tests, OK (expected failures=3), cov 99.77% (10166 stmts,
  5 missed, 1388 branches, 21 partial), test exit 0, coverage exit 0
- postgresql: Ran 2067 tests, OK (expected failures=3), cov 99.77% (10166
  stmts, 5 missed, 1388 branches, 21 partial), test exit 0, coverage exit 0

Artifact: `scripts/figures/TIER1-THROTTLE-CACHE.json`. The 5 missed statements
are the pre-existing `orders/views/payment.py` block (unchanged from base).
`manage.py makemigrations --check`: No changes detected.

## Skip semantics (why two pins can be unarmed)

The two live-Redis pins skip loudly (message names `REDIS_URL`) when no Redis
is declared or reachable, so a Redis-less CI stays green rather than red on a
service it does not have. In any run with `REDIS_URL` declared they execute.
CI note for the release-engineer: add a Redis service + `REDIS_URL` env to the
backend workflow to arm them there.
