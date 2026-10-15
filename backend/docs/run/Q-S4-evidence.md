# Q-S4 evidence — F8 verify-and-harden: MFAAdminSite enforcement + admin login throttle

Branch `lane/b`, worktree `wt-async`, 2026-10-15. All runs: workdir `backend`,
`venv\Scripts\python -m coverage run manage.py test --verbosity 0` +
`coverage report`. Floor figures below are printed by
`venv\Scripts\python ..\scripts\changelog_figures.py floor --task Q-S4`
(GATE-7), artifact `scripts/figures/Q-S4.json`. The PostgreSQL leg used lane
B's container `pg2b` (`postgres://…@localhost:5433/perfume_store_b`, passed
as a process env var for the one command, never typed); the script itself
probes and measures the engine (`django.db.backends.postgresql` /
`perfume_store_b`) and refuses to label a run the probe disagrees with.

## Floor, pre- and post-change

| When | Engine | Result | Coverage |
|---|---|---|---|
| Pre-flight (tree untouched) | SQLite | `Ran 2101 tests`, `OK (skipped=2, expected failures=3)` | TOTAL 99.81%, 10370 stmts, 0 missed |
| Post-change (floor script) | SQLite | `Ran 2111 tests`, `OK (expected failures=3)`, test exit 0, coverage exit 0 | 99.81% (10409 stmts, 0 missed, 1424 branches, 22 partial) |
| Post-change (floor script) | PostgreSQL 17 (`pg2b`) | `Ran 2111 tests`, `OK (expected failures=3)`, test exit 0, coverage exit 0 | 99.81% (10409 stmts, 0 missed, 1424 branches, 22 partial) |

Test count grew by exactly the ten new pins; expected-failure count (3) and
skip count (2) unchanged; coverage TOTAL unchanged at 99.81% with 0 missed
statements (`accounts/admin.py` itself 100%). `makemigrations --check` clean;
`ruff check .` and `ruff format --check .` clean over the whole tree
(214 files).

## Enforcement matrix proved (path x enrollment state x result)

Enrollment state: *pre* = privileged account, no device; *post* = active
enabled device. Cells backed by tests in `backend/tests/test_mfa.py`
(pre-existing cells marked ✓, new cells marked NEW):

| # | Path / request | Pre | Post |
|---|---|---|---|
| 1 | GET `/admin/login/` | 200, code field rendered, enrollment link always offered ✓ | 200, code field rendered, no secret ✓ |
| 2 | POST password only | REFUSED: "Multi-factor authentication is mandatory…" + enrollment notice ✓ | REFUSED: "Enter your authentication code." ✓ |
| 3 | POST password + wrong code | enrollment gate fires first ✓ | REFUSED: "Invalid or expired authentication code." ✓ |
| 4 | POST password + valid code | still REFUSED (enrollment required) ✓ | ACCEPTED (redirect, session set) ✓ |
| 5 | POST wrong password + valid code | — | REFUSED, uniform credential error, no MFA hint (NEW) |
| 6 | POST unknown user + valid code | — | REFUSED, uniform credential error, no MFA hint (NEW) |
| 7 | POST password + same valid code twice | — | second REFUSED (replay watermark) ✓ |
| 8 | POST with trusted-device marker (SPEC-20-8b) | — | skips fresh code; every fail-safe still challenges ✓ |
| 9 | Roleless staff / customer POST | unaffected / refused at the door ✓ | — |
| 10 | Throttle: budget spent, then any POST above | — | REFUSED with the one generic `ADMIN_LOGIN_THROTTLE_MESSAGE`, before the first factor is evaluated (NEW) |
| 11 | Throttle: failures per REMOTE_ADDR ident | second ident unaffected while first is blocked (NEW) |
| 12 | Throttle: window lapses | budget restored (hermetic clock, NEW) |
| 13 | Throttle: successes | spend nothing — two successful logins inside a 2/min budget, then a failure still inside it (NEW) |
| 14 | Throttle: `THROTTLE_ADMIN_LOGIN_RATE=""` | bound disabled end-to-end (NEW) |
| 15 | `/api/accounts/mfa/setup|confirm/` unauthenticated | — | 401 uniform ✓; **auth budget engages: 3rd attempt 429, confirm shares the counter** (NEW) |
| 16 | `/api/accounts/mfa/setup|confirm/` non-privileged session | — | 403 ✓ |
| 17 | API `auth` budget exhausted (429 at `/api/accounts/login/`) vs `/admin/login/` | **admin form still evaluates and admits** — the API scope provably never covered the plain-Django door (NEW; this is the gap the new throttle fills) |

## What was added, and why

- `backend/accounts/admin.py`: `AdminLoginThrottle` (subclass of DRF
  `SimpleRateThrottle`, scope `admin-login`) + `ADMIN_LOGIN_THROTTLE_MESSAGE` +
  the `clean()` wrapper (`_clean_with_mfa` holds the untouched MFA body).
  Check-before/record-on-failure: a spent budget is refused before the
  password check; every ValidationError (wrong password, missing/invalid code,
  enrollment required) spends exactly one unit; success spends nothing; no
  request on the form (direct construction in tests) steps aside. Keyed per
  client ident via `get_ident` (NUM_PROXIES-aware X-Forwarded-For, else
  REMOTE_ADDR) — the same trust decision DRF makes for anonymous budgets.
- `backend/config/settings.py`: `ADMIN_LOGIN_THROTTLE_RATE = os.getenv(
  "THROTTLE_ADMIN_LOGIN_RATE", "10/min")` — env-driven, same `n/min` shape as
  the DRF rates, empty disables.
- `backend/.env.example`: the knob documented beside the other throttle rates.
- `backend/tests/test_mfa.py`: ten new pins (classes `AdminMfaPathMatrixCells`,
  `AdminLoginThrottleTests`, `AdminLoginThrottleSettingsTests`,
  `AdminLoginThrottleNonCoverageTests`, `MfaEnrollmentThrottleTests`), plus a
  class-level `override_settings(ADMIN_LOGIN_THROTTLE_RATE="1000/min")` on the
  three pre-existing admin-login classes so their exact-MFA refusals can never
  be answered by the throttle's generic one (their assertions are unchanged).
  No existing assertion was weakened; no test was deleted or skipped.

Why not reuse the API auth scope instead: DRF's `ScopedRateThrottle` only ever
runs inside DRF views, and the admin login is `AdminSite.login` -> a plain
Django `LoginView`. Cell 17 measures that gap directly (exhaust the `auth`
budget at the API door, then show `/admin/login/` still admitting the same
ident), so the added mechanism is proved necessary rather than assumed.

## Mutation proof (each run reverted; tree byte-identical after, `git diff`
back to the 92-insertion state)

| Mutation | Result |
|---|---|
| M1 `AdminLoginThrottle.blocked` neutered to always return False | exactly the three budget-dependent pins fail by name: `test_spent_budget_refuses_further_attempts_with_one_generic_message`, `test_the_budget_is_per_client_ident`, `test_the_budget_restores_when_the_window_lapses` (5 run, 3 failed) |
| M2 `AdminLoginThrottle.record_failure` neutered to a no-op | the same three pins fail by name (5 run, 3 failed) |

The success/kills-switch/settings pins pass under both mutations, as they
should: they pin the absence of the guard's effect on honest traffic and the
knob contract, not the guard itself.

## Findings belonging to other tasks (reported, not fixed here)

- `backend/docs/vulnerabilities.md` V-18 still reads "Admin at default URL,
  no 2FA/lockout", status OPEN: the 2FA half shipped with SPEC-17-05 and the
  lockout/throttle half with this task. Row is stale — owner of that file
  (auditor/human) should re-derive and close or re-scope it.
- `backend/docs/decisions.md` F8 bullets ("Verify MFAAdminSite actually
  enforces…", "Ride-along backend task: throttle/lockout…") are now
  discharged by this task; the orchestrator owns that doc.
- Pre-existing untracked file `backend/docs/run/TIER1-THROTTLE-CACHE-pre-redis.txt`
  in the worktree — not created or touched by this task.
