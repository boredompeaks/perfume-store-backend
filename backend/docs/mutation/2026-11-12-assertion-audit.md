# Assertion audit — tests that only agreed with themselves (2026-11-12)

A test can be green for the wrong reason. This pass looked for assertions whose
truth does not depend on the code they claim to pin, then strengthened each one
and proved the change by mutation testing on PostgreSQL 17. Every finding below
was reproduced by making the product defect the test claims to catch; the old
assertion stayed green, the strengthened one went red.

This is an audit of test strength, not of product behaviour: no product file is
changed by this work. The one non-assertion change is the clock freeze recorded
in section 7, which a full-suite run forced (the probe had become
calendar-dependent; it is a test-only fix and is unrelated to the audit).

## 1. Method

1. Read each test next to the product code it calls, and ask one question:
   *if this branch were wrong, would this assertion fail?* The candidates were
   assertions that (a) derive their expected value from the value under test,
   (b) evaluate a "before" snapshot after the action, (c) claim a format but
   assert a weaker property, or (d) iterate a collection that may be empty.
2. For every candidate, write at least five hand-built mutants in the product
   path the test exercises, run the *named test only* at the old assertion
   (test files reverted to the parent commit) and at the strengthened
   assertion, and record kill/survive.
3. A mutant that survives the old assertion is the defect report. A mutant
   that the strengthened assertion kills is the fix.
4. Run the full suite and branch coverage on PostgreSQL before and after; run
   the changed files through the pinned ruff.

The mutants are deliberately small and specific to the invariant: they are
evidence that *this assertion* pins *this* branch, not a claim that the whole
suite is mutation-complete. Mutant scope and the pre/post code are in the
"Mutation matrix" section.

## 2. Findings

| # | Test (file) | What it claimed | Why it did not pin it | Fix |
|---|---|---|---|---|
| 1 | `test_reading_the_listing_moves_no_money` (`backend/orders/tests_returns.py`) | Reading the returns listing/detail moves no money. | `before` was a **lazy queryset**. `list(before)` was evaluated inside the assertion, after both GETs, so a GET that mutated money changed both sides and the equality still held. | Materialise and order the snapshot before the reads; assert the fixture is non-empty and has the expected money shape; compare the materialised snapshot after. |
| 2 | `test_the_amounts_are_decimal_strings_not_json_numbers` (`backend/shipping/tests.py`) | Shipping amounts are two-decimal wire strings, not JSON numbers. | `assertEqual(amount, str(Decimal(amount)))` accepts `"99"`, `"1E+2"` and `"99.0000"`: it pins "canonical Decimal rendering", not two decimal places. The loop is also vacuous if `options` is empty. | Assert the exact option amounts as literals, assert three options, and require `^\d+\.\d{2}$`. |
| 3 | `test_the_estimate_never_carries_a_row_id` (`backend/shipping/tests.py`) | Every serialized option carries exactly the four public keys. | `for option in res.data["options"]` passes with zero options, so a broken/empty estimate was green. | Assert the option count before the loop. |
| 4 | `test_the_timestamp_is_fixed_width_and_sorts_chronologically` (`backend/tests/test_backup_db.py`) | Backup dump stamps are fixed-width and sort chronologically. | Comparing the lengths of two samples and their order does not pin the format: dropping `Z`, swapping separators, `%y`, `%j` and `_` all keep equal length and correct order. | Assert the two exact stamps, a format regex, and the year-rollover ordering boundary. |
| 5 | `test_sync_twice_leaves_exactly_one_group_per_role_with_stable_names` and `test_sync_recreates_a_missing_role_group` (`backend/tests/test_rbac_foundation.py`); `test_the_group_exists_on_a_bootstrapped_database` and `test_sync_recreates_a_deleted_superadmin_group` (`backend/tests/test_superadmin_tier.py`) | Staff role Group names are stable. | The expected names were built from `STAFF_ROLES` / `ROLE_*`, the same constants `sync_role_groups()` creates the groups from. Renaming a role constant moves both sides: the new test database gets the new name and the assertion agrees, while deployed databases keep the old group and its members lose the role. | Pin the seven names as literals in the sync tests; add `StaffRoleWireLiteralTests` for the constants and `STAFF_ROLES` tuple. |
| 6 | `test_an_absurd_attempt_count_cannot_build_an_absurd_backoff` (`backend/tests/test_notification_outbox_worker.py`) | The backoff exponent is clamped, so a huge attempt count cannot build a huge delay. | The assertion compared `retry_delay_seconds(10**9)` with `retry_delay_seconds(_BACKOFF_EXPONENT_CAP + 1)`. That is determinism, not the cap: moving the clamp or the configured cap moves both calls and the equality survives. | Pin the literal values the operator gets: 30s at attempt 1, 60s at attempt 2, 3600s at the cap and at `10**9`. |
| 7 | `test_same_database_yields_the_same_key` (`backend/tests/test_test_isolation.py`) | The suite lock key is derived from the resolved database (engine, host, port, test name), not its spelling. | The assertion was `identity(x) == identity(x)`. Any pure function, including one that hashes the wrong fields or truncates to the wrong width, is equal to itself, so the composition was unobservable. | Pin the literal key for the sample PostgreSQL config; the key is SHA-256 of `engine:host:port:test name` truncated to 16 hex characters. |

Severity of the findings: 1, 5 and 7 can hide a real money or access-control
regression while the suite is green; 2 and 4 underspecify wire contracts
(amounts and dump filenames); 3 and 6 let an empty/broken path and a wrong
policy constant pass unnoticed.

### 2.1 Triage of the mutations that the strengthened assertions kill

All mutants listed in section 3 are killed by the strengthened assertions. The
triaged near-misses are recorded here so they do not read as coverage:

- `DEFAULT_RETRY_BASE_SECONDS` / `DEFAULT_RETRY_MAX_SECONDS` in
  `backend/common/notifications.py` are **equivalent mutants under this test**:
  settings always declares `NOTIFICATION_OUTBOX_RETRY_*`, and `_setting()` reads
  the settings attribute before its fallback, so changing the module default is
  unobservable at runtime. The retry mutants therefore target the live settings
  defaults and the exponent clamp instead.
- The role-name mutant on `ROLE_CATALOGUE` is already killed by the pre
  assertion set: `test_the_six_operational_roles_keep_their_place_below_it`
  spells `catalogue` as a literal inside its ordered tuple. The other four
  constants in the matrix (`support`, `inventory`, `admin`, `superadmin`)
  survive the scoped pre label; the full suite happens to kill `support` too
  through the admin audit-message literals, while `admin` and `superadmin`
  have no literal pin anywhere. The new `StaffRoleWireLiteralTests` closes all
  seven regardless of which other test happens to cover a name.
- Two mutants from the first calibration run were discarded: one mutated a
  module default that settings shadows (above), and one was applied while a
  previous harness process had left its own mutation on disk. The matrix below
  was produced by a preflight-guarded run that refuses to start on a dirty
  product tree and restores the file on SIGTERM/SIGINT.
- The runner also clears `__pycache__` before every subprocess. A mutant and
  its restore can land inside one whole second, and CPython's `.pyc` header
  records the source mtime only in seconds with the source size; a bytecode
  file compiled from a mutant can therefore still validate against the
  restored source of the same size. A calibration full-suite run did exactly
  that and saw the mutant's `_` separator through a restored file, which is
  why bytecode is now invalidated per run.

## 3. Mutation matrix

Each row is one hunk: the named test run against the old assertion (`pre`) and
the strengthened assertion (`post`), with the same product mutants. `S` =
survived, `K` = killed. A `pre` S followed by a `post` K is the proof the old
assertion did not pin the branch and the new one does.

| Hunk | Named test | Mutants | pre survived | pre killed | post killed |
|---|---|---|---|---|---|
| `returns_snapshot` | `test_reading_the_listing_moves_no_money` | 5 | 5 | 0 | 5 |
| `shipping_amount` | `test_the_amounts_are_decimal_strings_not_json_numbers` | 5 | 5 | 0 | 5 |
| `shipping_loop` | `test_the_estimate_never_carries_a_row_id` | 5 | 5 | 0 | 5 |
| `backup_timestamp` | `test_the_timestamp_is_fixed_width_and_sorts_chronologically` | 5 | 5 | 0 | 5 |
| `role_names` | `StaffRoleWireLiteralTests` (post) / the two sync classes (pre) | 5 | 4 | 1 | 5 |
| `retry_cap` | `test_an_absurd_attempt_count_cannot_build_an_absurd_backoff` | 6 | 6 | 0 | 6 |
| `db_identity` | `test_same_database_yields_the_same_key` | 5 | 5 | 0 | 5 |
| **total** | | **36** | **35** | **1** | **36** |

The mutant families, one per hunk: money writes on the returns read path
(`payment_status`, `refunded_at`, `total_amount`, row deletion); shipping
`decimal_places` 0/1/3/4/5; option-list truncation and emptying; backup stamp
format variants (drop `Z`, dashed separators, `%y`, `%j`, underscore);
five staff-role constant renames (`support`, `catalogue`, `inventory`, `admin`,
`superadmin`); retry backoff exponent clamps and live settings defaults; and
database-identity composition changes (host/port order, trailing separator,
engine shortening, `test_` prefix, 16-hex truncation).

`pre` at 35/36 and `post` at 36/36 is the result the audit predicts: one role
mutant is already killed by an incidental assertion in the old class (see
section 2.1), while every mutant that the old assertions let through is killed
by the strengthened ones. There are **0 post survivors**.

Every hunk has at least five mutants.

## 4. Verification

- PostgreSQL 17 (container `postgres:17-alpine`), `DATABASE_URL` pointing at a
  local test database; engine is the authoritative CI engine.
- Full suite (strengthened tree): **2540 tests, OK** (4 skipped, 2 expected
  failures), 181.5s.
- Full suite (parent commit plus only the clock freeze, old assertions):
  **2538 tests, OK** (4 skipped, 2 expected failures), 176.3s. The two-test
  difference is `StaffRoleWireLiteralTests`.
- Branch coverage on the same runs: baseline **1833/1862** branches covered
  (98.44%, 29 partial) and post **1833/1862** (98.44%, 29 partial) — no branch
  was lost. Statement coverage is 99.76% on both; the post run covers three
  more statements (14402/14412 vs 14399/14409), all in
  `backend/orders/tests_returns.py`, which `.coveragerc` measures because
  app-level test modules sit inside `source = .`. Product-code misses are
  unchanged at 10 lines.
- `ruff check` and `ruff format --check` on the seven changed test files: clean.
- `scripts/doc_claims.py` on this document: clean.

## 5. Limits and non-goals

- The mutants are hand-written per hunk, not an exhaustive operator sweep; a
  green matrix means these branches are pinned, not that the suite is
  mutation-complete.
- Kill evidence is scoped to the named test. Another test in the suite may kill
  the same mutant; that does not make the weakened assertion acceptable, which
  is why the pre leg reverts only the audited file(s) and runs the named test.
- Display labels, docstrings and class names are not pinned by this pass unless
  a deployed contract depends on the literal (the role-group names are such a
  contract; admin display labels are not).
- No product code changed. The clock freeze in section 7 is test-only.

## 6. Change list

Seven test files, no production files:

- `backend/orders/tests_returns.py` — eager snapshot and fixture-shape asserts
  in `test_reading_the_listing_moves_no_money`; clock freeze for the years-ago
  probe.
- `backend/shipping/tests.py` — exact amount literals, two-decimal regex, and
  non-empty option lists.
- `backend/tests/test_backup_db.py` — exact dump stamp literals, format regex,
  rollover boundary.
- `backend/tests/test_notification_outbox_worker.py` — literal backoff values.
- `backend/tests/test_rbac_foundation.py` — literal role names in the sync
  tests and a new `StaffRoleWireLiteralTests`.
- `backend/tests/test_superadmin_tier.py` — literal group names in the
  bootstrap/sync pins.
- `backend/tests/test_test_isolation.py` — literal database-identity key.

## 7. Test-only clock fix (out of audit scope)

`test_a_request_filed_years_ago_is_still_readable_and_still_its_own`
(`backend/orders/tests_returns.py`) stamped its order against the class's fixed
`NOW` but filed the request against the wall clock. Once the real date passed
`NOW + 30 days`, the 30-day window refused it with 409 and the probe stopped
testing readability. Filing now runs inside the class's existing
`clock_frozen_at()` seam. This is a test-code fix found by the full-suite run,
not an assertion-strength finding, and it has no mutation row of its own.
