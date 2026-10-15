# Q-S3 evidence — ACCESS GATE on username-available + register (backend half)

Raw stdout of each run is kept beside this file as the `Q-S3-*.log` files;
this repo does not track raw suite logs (precedent: other lanes' run logs are
local artifacts too), and every summary line quoted below is verbatim from
the log of the same name.

Branch `lane/b`, worktree `wt-async`, 2026-10-15. All runs: `workdir backend`,
`venv\Scripts\python -m coverage run manage.py test --verbosity 0` +
`coverage report`. PostgreSQL legs run with `DATABASE_URL` passed as a process
env var for the single command, redacted here as
`postgres://…@localhost:5433/perfume_store_b` (lane B container `pg2b`); the
engine was probed BEFORE each leg with a `select version()` cursor call and
answered `ENGINE: django.db.backends.postgresql NAME: perfume_store_b`,
`PostgreSQL 17.11` — settings fall back to SQLite silently when the URL is
unset, so the green summary alone is not the engine proof.

## Pre-flight baseline (tree unchanged, three runs — the floor is flaky)

| Run (UTC) | Engine | Result | Coverage |
|---|---|---|---|
| 02:45–02:54 | SQLite | `Ran 2093 tests`, `FAILED (failures=1, skipped=2, expected failures=3)` — the failing test was `test_the_purge_is_a_daily_sweep_not_a_sub_daily_one` | TOTAL 99.81%, 10346 stmts, 0 missed, exit 0 |
| 03:20–03:26 | SQLite | `Ran 2093 tests`, `FAILED (failures=2, skipped=2, expected failures=3)` — failure names not captured (output piped through a tail) | TOTAL 99.81%, exit 0 |
| 03:33–03:39 | SQLite | `Ran 2093 tests`, `FAILED (failures=1, skipped=2, expected failures=3)` — failing test `test_failing_drill_cleans_up_exactly_like_a_passing_one`, log `Q-S3-baseline-sqlite.log` | TOTAL 99.81%, 10346 stmts, 0 missed, exit 0 |
| 03:45–03:52 | PostgreSQL 17.11 | `Ran 2093 tests`, `OK (skipped=2, expected failures=3)`, log `Q-S3-baseline-pg.log` | TOTAL 99.81%, 10346 stmts, 0 missed, exit 0 |

Pre-existing intermittent failures, observed on the UNCHANGED tree and NOT
fixed here (out of scope; reported for orchestrator routing):

1. `test_the_purge_is_a_daily_sweep_not_a_sub_daily_one` — deterministic clock
   window: it asserts `remaining > 3600` for the daily 03:17 UTC purge, so it
   fails on every full-suite run that executes it between 02:17 and 03:17 UTC
   (run 1 started inside that window; runs 2–3 after it closed and passed it).
2. `test_failing_drill_cleans_up_exactly_like_a_passing_one` — timing flake:
   the drill subprocess's scratch directory
   (`restore-drill-*` under the OS temp dir) was still present when the
   before/after set comparison asserted (run 3).

## Post-change verification (same commands, after the edits)

| Engine | Result | Coverage |
|---|---|---|
| SQLite | `Ran 2101 tests`, `OK (skipped=2, expected failures=3)`, exit 0, log `Q-S3-after-sqlite.log` | TOTAL 99.81%, 10370 stmts, 0 missed, 1414 branches, 22 partial, exit 0 |
| PostgreSQL 17.11 (probe: `django.db.backends.postgresql` / `perfume_store_b` / `PostgreSQL 17.11`) | `Ran 2101 tests`, `OK (skipped=2, expected failures=3)`, exit 0, log `Q-S3-after-pg.log` | TOTAL 99.81%, 10370 stmts, 0 missed, 1414 branches, 22 partial, exit 0 |

Zero-regression read: test count grew from 2093 to 2101 — exactly the eight
new `AccessChallengeTests` methods — the expected-failure count is unchanged
on both engines, no failure names appear in either after-run, coverage
percentage held with 0 missed lines (statement total grew with the new code),
and the new code is fully exercised.

## New pins (accounts/tests.py, class AccessChallengeTests unless noted)

- `test_zero_config_default_requires_no_challenge` — unset secret: both
  endpoints answer with no challenge parameter; pins the shipped defaults of
  both env knobs.
- `test_missing_challenge_is_refused_identically_on_both_endpoints` — same
  403 envelope on register and probe; no account, no mail.
- `test_valid_signed_challenge_admits_both_endpoints`,
  `test_challenge_rides_the_query_string_too` — both carriers admit a valid
  token.
- `test_bad_tokens_all_share_one_refusal` — missing, garbage, foreign-secret,
  wrong-payload, tampered, non-string: one refusal body (six subtests; each
  subtest clears the throttle cache because six requests exceed the default
  five-per-minute register budget — which is itself the tightening engaging).
- `test_stale_challenge_is_refused` (the signer's own clock reference is
  patched so the mint carries a backdated stamp),
  `test_max_age_below_one_keeps_only_the_signature_required`.
- `test_the_throttle_budget_still_gates_before_the_challenge` — a refused
  attempt consumes its scope budget; the second is 429.
- Repins: `test_response_never_returns_password` (exact `user` dict now
  username+email only, `assertNotIn("id", ...)`), `test_auth_scope_and_rate_are_configured`
  (register/username-probe scopes + literal default rates + tighter-than-auth
  invariant), `test_all_account_urlpatterns_declare_throttle_scopes` (expected
  map), `test_register_rate_limit_engages` and
  `test_username_available_rate_limit_engages` (patch the new scope keys).

## Mutation check (gate tests are not vacuous)

With `_enforce_access_challenge` temporarily neutered (`if not secret or True:
return`, one-line edit, reverted immediately after the run):
`Ran 76 tests`, `FAILED (failures=9)` — the six bad-token subtests, the
missing-challenge pin, the stale pin and the throttle-order pin all failed by
name. Restored; `git diff` shows the hook intact in the final tree.

## Gates

- `venv\Scripts\python -m ruff check .` → `All checks passed!`, exit 0.
- `venv\Scripts\python -m ruff format --check .` → `214 files already
  formatted`, exit 0 (the pinned 0.16.10 in the venv; my test file was
  formatted with it).
- `venv\Scripts\python manage.py makemigrations --check --dry-run` →
  `No changes detected`, exit 0 (no models added — none were needed).

## Residual enumeration oracles (demonstrable after this change)

Owner chose ACCESS GATE over endpoint removal (decisions.md F3), so these
remain by decision, each bounded by the tightened budgets and (once the owner
sets `ACCOUNT_CHALLENGE_SECRET`) by the challenge on both endpoints:

1. **The probe itself, zero-config**: with the secret unset (shipped
   default), `GET /username-available/` still answers existence directly —
   bounded only by the `username-probe` default budget. Demonstrated by
   `test_taken_username_case_insensitive` and
   `test_free_username_reports_available` (green post-change).
2. **Registration username-taken**: `POST /register/` answers 400 with
   `details.username` carrying "This username is already taken." where a free
   username answers 201 — a username oracle at registration time. Demonstrated
   by `test_duplicate_registration_rejected_before_smtp` and
   `test_duplicate_username_case_insensitive_rejected`.
3. **Registration email-taken**: `POST /register/` answers 400 with
   `details.email` carrying "An account already uses this email address."
   where an unused email answers 201 — an email-account oracle. Demonstrated
   by `test_duplicate_email_case_insensitive_rejected`.

Not oracles (pinned uniform elsewhere and untouched here): the three recovery
flows' bodies (`test_recovery_throttling_preserves_uniform_bodies`), and the
403/429 gate responses, which name no account state.
