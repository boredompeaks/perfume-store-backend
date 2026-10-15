# SPEC-Q-D7 evidence — the two pre-existing environmental test reds

Branch `lane/b`, worktree `wt-async`, 2026-10-15, base `3b5cf88`. All runs:
`workdir backend`, test commands exactly `venv\Scripts\python -m coverage run
manage.py test --verbosity 0` + `venv\Scripts\python -m coverage report`
(here executed through `scripts/changelog_figures.py floor`, which runs those
two commands per engine and refuses a leg whose engine probe resolves to the
wrong vendor). PostgreSQL legs: `DATABASE_URL` passed as a process env var
for the single command, lane B container `pg2b` on localhost:5433, database
`perfume_store_b`, password redacted; the script's own probe named the
backend `django.db.backends.postgresql` before measuring. No network in any
test. No new dependency: the freeze uses the repo's `unittest.mock`
(freezegun and time-machine are not installed in the venv — checked with
`pip list`).

## Pre-flight (tree unchanged, before any edit)

- SQLite: `Ran 2114 tests in 346.074s`, `OK (skipped=2, expected failures=2)`,
  coverage TOTAL 99.81%, 10423 stmts, 0 missed, exit 0. This is the clean-floor
  snapshot; current UTC at start was ~14:33, i.e. outside the cadence window.

## Root causes re-derived this session (not carried forward)

1. **Cadence window — two clock reads, not one.** Celery's
   `crontab.remaining_estimate(last_run_at)` computes
   `last_run_at + next-fire-delta − schedule.now()`; the test supplied the
   first clock (`_utcnow()`) and the schedule's `nowfun`/`app.now` supplied
   the second, real one. Measured: with only the argument frozen to
   02:30 UTC the estimate is `-11791785.357642` seconds (frozen instant
   minus real now), so a freeze that misses the second clock is worse than
   none. With BOTH forced to 02:30 UTC the estimate is `2820.0` (02:30 →
   03:17), which the pin's `remaining > 3600` cannot satisfy — the
   deterministic 02:17–03:17 UTC daily red.
2. **Top-of-hour pin read its own seed.** Measured on the unmodified module:
   `_next_fires(..., count=2)` returned exactly ONE element for the purge
   and one for the reservation sweep — `[now + 20s]`, the walk's first
   cursor — because `is_due` could never fire again once `last_run` was
   parked at `now + 20s`. The minute the pin asserted was therefore the
   minute of "now + 20s", never a scheduled fire; it went red whenever a run
   crossed a minute boundary at :59:40–:59:59 (a second, rarer wall-clock
   red in the same class), and a mutation of the purge's minute could not
   have been caught by it.
3. **Restore-drill race — a shared mutable root.**
   `DrillHygieneAssertionsMixin` snapshotted
   `Path(tempfile.gettempdir()).glob("restore-drill-*")` and asserted set
   equality after each run. Any concurrently running suite that created or
   removed such a directory mid-test flipped the sets. Both lane-B builders
   (Q-S1, Q-S3) hit this independently; Q-S3's log shows the
   `test_failing_drill_cleans_up_exactly_like_a_passing_one` failure on an
   unchanged tree.

## Pre-fix reproductions (both red, both commands run before any edit)

**(a) Wall clock forced into 02:17–03:17 UTC** — in-process driver patched
the module's `_utcnow` AND `celery_app.now` to 2026-06-01 02:30 UTC, then ran
only `CadenceTests.test_the_purge_is_a_daily_sweep_not_a_sub_daily_one`:

```
AssertionError: 2820.0 not greater than 3600
Ran 1 test in 0.005s — FAILED (failures=1)   WINDOW-REPRO exit-failures: 1
```

**(b) Shared-temp churn during the drill module** — a second process created
`restore-drill-hammer-*` directories in %TEMP%, held each 150 ms (long enough
to straddle snapshot→assert), deleted it, for the whole run of
`manage.py test tests.test_restore_drill`:

```
Ran 26 tests in 46.087s — FAILED (failures=5)
```

All five failed in `assert_no_drill_workdirs_left`, each mismatch naming a
foreign `C:/Users/.../Temp/restore-drill-hammer-*` path:
`test_drill_round_trips_the_test_database_and_cleans_up`,
`test_failing_drill_cleans_up_exactly_like_a_passing_one`,
`test_refusal_leaves_no_scratch_connection_behind`,
`test_the_derived_test_database_is_accepted`,
`test_unusable_database_is_a_readable_refusal_not_a_traceback`.

## The fixes (test-side only; production untouched — `git diff --stat` names
exactly the two test modules until the changelog/evidence files are added)

- `backend/tests/test_celery_scheduling.py`: `_utcnow` deleted (no reference
  remains); `CADENCE_PROBE_INSTANTS` holds two frozen instants, one inside
  the old red window; `_frozen_remaining(schedule, when)` pins BOTH celery
  clocks (the `last_run_at` argument and the schedule's `nowfun`) to the same
  instant, so the estimate is a pure function of `when`. The drain and
  reservation pins assert their original bounds at both probes. The purge pin
  now asserts the fire-to-fire PERIOD (wait → stand on that fire → wait
  again; that second answer is the exact period): `> 3600` (not sub-daily)
  and `< NOTIFICATION_OUTBOX_TTL_SECONDS` (inside the TTL) — same claims the
  docstring always made, now phase-free. `_next_fires` was rewritten to
  render next-run times by the same frozen estimate (each fire = previous
  fire + its wait), so the top-of-hour pin reads real scheduled fires.
- `backend/tests/test_restore_drill.py`: `DrillHygieneAssertionsMixin.setUp`
  creates a per-test `drill-hygiene-scope-*` directory (prefix deliberately
  not matching `restore-drill-*`), redirects ONLY the drill's own
  `tempfile.mkdtemp(prefix="restore-drill-")` into it for the test's
  duration (every other `mkdtemp` call forwards unchanged), and
  `drill_workdirs()` globs only that scope. The shared OS temp root is no
  longer read or depended on; a foreign process cannot enter the set from
  which side either way.

## Post-fix proofs

**Window proof** — same forcing as repro (a): `celery_app.now` patched to
2026-06-01 02:30 UTC, all of `CadenceTests` run:

```
Ran 4 tests in 0.007s — OK   WINDOW-PROOF forced-02:30 failures: 0
```

The module no longer reads the real clock at all (its `_utcnow` seam is
gone); additionally the purge pin evaluates at a probe instant inside the
window by construction.

**Concurrency proof** — two suite processes ran
`manage.py test tests.test_restore_drill` SIMULTANEOUSLY while the hammer
process churned shared-temp `restore-drill-hammer-*` directories:

```
SUITE-A: Ran 26 tests in 55.833s — OK
SUITE-C: Ran 26 tests in 55.439s — OK
```

Hygiene after the run: 0 `drill-hygiene-scope-*` leftovers; 0 real
`restore-drill-*` drill workdirs in the shared root (the only matches were
two hammer directories whose process was killed mid-hold — my own artifact,
removed; the hammer is not part of the suite).

**Mutations (each reverted; `git status` afterwards shows only the two test
files)**

- M1+M2 together (purge `hour="*"` → period 3600, `minute="0"` → lands on
  the hour): `CadenceTests` → `FAILED (failures=4)`, both probes failing by
  name: `3600.0 not greater than 3600 : the purge must not be sub-daily`
  and `0 == 0 : the purge must not land on the hour`; the drain and
  reservation pins stayed green (failures scoped to the mutated pins).
  This also proves the rewritten top-of-hour pin is non-vacuous — the
  pre-fix walk, which asserted its own seed's minute, could not have caught
  `minute="0"`.
- M3 (`ops.management.commands.restore_drill.Command._remove_workdir`
  temporarily stubbed to a no-op):
  `test_failing_drill_cleans_up_exactly_like_a_passing_one` → `FAILED
  (failures=1)`, `AssertionError: Items in the first set but not the
  second:` naming the leaked workdir inside the scope — the scoped glob is
  load-bearing, not empty-by-construction.

## Floor pair (after the change; figures printed, not typed)

```
SPEC-Q-D7 floor — artifact scripts/figures/SPEC-Q-D7.json
- sqlite: Ran 2114 tests, OK (expected failures=2), cov 99.81% (10423 stmts, 0 missed, 1426 branches, 22 partial), test exit 0, coverage exit 0
- postgresql: Ran 2114 tests, OK (expected failures=2), cov 99.81% (10423 stmts, 0 missed, 1426 branches, 22 partial), test exit 0, coverage exit 0
```

Zero-regression read: test count equals the pre-flight snapshot (2114),
expected-failure count unchanged (2), skips unchanged (2), coverage TOTAL
unchanged at 99.81% with 0 missed lines, both engines exit 0.

## Gates

- `venv\Scripts\python -m ruff check tests/test_celery_scheduling.py
  tests/test_restore_drill.py` → `All checks passed!`, exit 0.
- `venv\Scripts\python -m ruff format --check` on both files → `2 files
  already formatted`, exit 0 (pinned ruff 0.16.10 in the venv).
- `venv\Scripts\python manage.py makemigrations --check --dry-run` → `No
  changes detected`, exit 0 (no models touched).
- `venv\Scripts\python ..\scripts\doc_claims.py --base 3b5cf88` and
  `venv\Scripts\python ..\scripts\changelog_figures.py check --base
  3b5cf88` → run after the changelog row; results recorded at commit time.

## Findings / proposals (reported, not fixed — outside this scope)

1. Pre-fix, the top-of-hour pin was vacuous against its own docstring (it
   asserted the seed's minute, not the crontab's). This task's rewrite of
   `_next_fires` makes it real; recorded because it was a pre-existing
   bias-trap, not something the Q-S1/Q-S3 root causes had named.
2. `RestoreDrillWorkdirHygieneTests.test_remove_workdir_deletes_every_file_it_contains`
   still creates a `restore-drill-cleanup-*` directory in the shared root.
   It is harmless under the new scoping (nothing globs that root anymore)
   and self-cleaning, but could ride the same per-process scope in a future
   test-infra pass.
3. Production code (`ops/management/commands/restore_drill.py`,
   `config/settings.py`, `config/celery.py`) was mutated only temporarily
   for the mutation proofs above and reverted; neither the cadence logic nor
   the drill was found buggy — the defects were entirely in how the tests
   read the clock and the temp root.
