# ASYNC-2c2 - AUDIT cycle 2 (RE-AUDIT, delta-only)

Branch `feat/async-2c2`, HEAD under audit `6eece42`, delta `13ec4d7..6eece42`
(the fix commit `ff23925` plus follow-up doc commit `6eece42`).

**Classification: `CYCLE ASYNC-2c2 -- CODE`** - the delta changes product
behaviour (a new alert type, a changed retry contract, a new import edge).

## What this cycle did NOT re-read, and why

Cycle 1's cleared axes are inherited as closed and were not re-derived: the
queryset enumeration, the call-site conversion question, the lock/join shape,
the envelope shape, the summary's nominal ordering, the placement of the four
expected-failure pins, and the changelog row's truthfulness at `13ec4d7`. This
cycle re-read the delta and attacked the complement of that surface. Nothing
found below contradicts any of those conclusions.

## Delta file set (derived from the diff, not from a handed list)

```
git diff --name-status 13ec4d7..6eece42
M  backend/common/management/commands/drain_notification_outbox.py
M  backend/common/notifications.py
M  backend/common/models.py
M  backend/ops/alerts.py
A  backend/templates/emails/alert_background_job_failure.txt
M  backend/tests/test_notification_outbox_worker.py
A  backend/docs/run/ASYNC-2c2-c2-evidence.md
M  backend/docs/changes.md
A  scripts/figures/ASYNC-2c2-c2.json
```

## Ledger tamper check (step 0, first)

`git diff --name-only 13ec4d7..6eece32 -- spec-run-state.md docs/run/section-*`
-> **no ledger file in the delta**. The `## Baseline` block compared between
`13ec4d7:backend/docs/spec-run-state.md` and `6eece42:...` is **byte-identical**.
CLEAN.

## Lane isolation

Connection details were never typed or read: `pg-lane.ps1 -Lane A -Verify` was
invoked for its stdout only (it prints engine + test-database name), and
`DATABASE_URL` was set and removed per command. The helper file's contents were
never read, so no credential appears in this file. `-Lane B` was never used from
this worktree. The worktree's `.env` hard link was never written.

`-Lane A -Verify` output: `lane A container pg2a port 5432 engine
django.db.backends.postgresql name perfume_store test database will be
test_perfume_store`.

Engine was additionally confirmed from inside the process on every PG leg
(`config.settings.DATABASES['default']['ENGINE']` -> `django.db.backends.postgresql`).
This mattered: one earlier PG attempt silently fell back to SQLite because the
nested helper call had failed, and reporting that number as PostgreSQL would
have been wrong.

## The floor - a pair, both legs measured in this session

**SQLite** (concurrent with the second lane, then the contested subset re-run in
isolation):

    Found 1912 test(s).
    Ran 1912 tests in 248.813s
    OK (expected failures=4)
    coverage: TOTAL 9322 stmts, 0 missed, 1358 branches, 21 partial, 99.80%

**PostgreSQL** (lane A, in isolation, engine verified inside the process):

    ENGINE: django.db.backends.postgresql
    Ran 1912 tests in 262.963s
    FAILED (failures=6, errors=1, expected failures=4)
    coverage: TOTAL 9322 stmts, 0 missed, 1358 branches, 21 partial, 99.80%

Every figure in the delta's own `scripts/figures/ASYNC-2c2-c2.json` was
independently reproduced above (1912/4, 1912/6/1/4, 9322, 0 missed, 1358, 21
partial, 99.80%). No figure was transcribed.

PG red names, all pre-existing and outside this delta's file set:
`products.tests.SlugGenerationTests.test_long_name_collision_truncated_to_slug_max_length`
(the only product-code defect, ledgered **PG-2b**), plus
`orders.tests.OrderIndexSchemaTests.test_order_number_stays_satisfied_by_its_unique_constraint`,
`orders.tests.OrderIndexSchemaTests.test_payment_provider_references_stay_constraint_covered`
(x2), `products.tests.ProductCatalogIndexTests.test_slug_stays_satisfied_by_its_unique_constraint`,
`tests.test_correlation_ids.UnhandledExceptionTests.test_unhandled_exception_500_still_returns_the_request_id`,
`tests.test_e2e_concurrency.PaginationStabilityTests.test_pages_partition_the_catalog_stably_across_repeated_requests`
-> ledgered **PG-2e** (test-only). None is in `common/`, `ops/alerts.py` or the
outbox test module.

## Contention, measured not charged

The first pass on both engines showed 2 failures each, all in
`tests.test_restore_drill` (`AssertionError` on a leaked
`%TEMP%\restore-drill-*` workdir). Re-run **in isolation**:

    venv\Scripts\python -m coverage run manage.py test tests.test_restore_drill
    Ran 26 tests in 51.455s
    OK

That is a shared-machine artifact of the second lane running concurrently, not a
regression. The suite figure quoted above is the clean one.

**Flake protocol for the known two-thread claim race.** The builder's row claims
a pre-existing flake in the two-worker race class that this cycle does not touch.
Attempted reproduction, per the standing rule that a timing failure is never
charged on one observation:

    SQLite, OutboxTwoWorkerTests, 5 isolated runs: OK, OK, OK, OK, OK
    PostgreSQL (engine verified), 5 isolated runs: OK, OK, OK, OK, OK

**10/10 green. The claimed flake is UNREPRODUCIBLE in this session.** It is not
adjudicated here as a defect in either direction; the builder's characterisation
simply could not be observed, and no run was charged as a regression.

## The four findings - each closed by breaking it on purpose

All mutations were applied to a throwaway copy at
`%TEMP%\opencode\a2c2c2\mut` (venv reached through a junction). **The worktree
was never mutated.** Every restore verified byte-identical by SHA-256.

Control on the copy before mutating: the 4 delta-relevant classes (20 tests) -> OK.

### BUG-1 [P2] alerting on repeated failures - CLOSED, mutation-proven

Broke it by deleting **both** `_alert_dead_letter(...)` call sites (the two
transitions enumerated separately, because naming two sites is not enumerating
them):

    both call sites removed  -> OutboxDeadLetterAlertTests: FAILED (failures=3)
    only the _dead_letter call removed -> FAILED (failures=2)
      (test_a_row_that_can_never_be_resolved_raises_an_admin_alert,
       test_repeated_failures_alert_again_once_the_cooldown_lapses)
    only the _record_failure call removed -> FAILED (failures=1)
      (test_a_row_that_exhausts_its_attempts_raises_an_admin_alert)

Restore hash `55AB6BE1...570E` == original. **Both transitions are independently
pinned.**

Complement checks on the alert module (the instruction was narrow - add the type
and the call, do not touch the cooldown):

- `_in_cooldown`, its call site in `_send`, the `ALERT_COOLDOWN_SECONDS` read and
  the cache backend are **absent from the delta** - confirmed by reading the
  `ops/alerts.py` diff hunk-by-hunk. The only changes to that module are the new
  constant, the new function, and one module-docstring line ("the four alert
  types" -> "the alert types") which is now *more* accurate.
- The new type does **not** inherit the per-worker cooldown defect as a false
  claim. Measured, with a control that fires:

      single process, burst of 3 notify calls  -> 1 send   (cooldown held)
      three separate processes, same burst    -> 3 sends  (did NOT hold)

  That is the ledgered **ASYNC-2d** P2 (per-process `LocMemCache`, no `CACHES`
  configured; `section-00-infrastructure.md:135` and `spec-run-state.md:45`).
  It is **pre-existing and owned by another task** - this delta neither introduces
  nor worsens it. The new docstring's claim ("a burst of poison rows is one mail")
  is scoped to the management-command trigger site, and I measured that a burst
  *in one command process* does coalesce to one mail. The overclaim risk the brief
  named - a docstring reading as though a cross-worker cooldown now holds - is
  **not** present: the sentence does not claim cross-process suppression. No
  finding.
- Fail-open verified: a cache backend that raises on `get`/`set` does not escape
  `notify_background_job_failure` (returns True, alert attempted, exception
  logged) - the drain cannot be killed by an alert.
- No recipients configured -> returns False, row still dies.

### BUG-2 [P2] manual retry reported a success it could not deliver - CLOSED, mutation-proven

Broke it by reverting the refusal to the cycle-1 behaviour
(`expired_pks = ()`, `requeued = doomed.update(...)` - no expiry filter):

    OutboxRetryRetentionTests: FAILED (failures=4)

and the mutant's own output reproduced cycle 1's defect verbatim:

    're-opened 0 dead row' not found in '... re-opened 1 dead row(s), 0 left dead
     past their expiry; backlog is pending 1, sent 0, failed 0, dead 0.'

`backlog ... dead 0` on a row that is plainly dead - exactly the erased signal
cycle 1 measured. Restore hash identical.

**The builder's reasoning, judged on its merits against the requirement.** Both
answers are defensible; the refusal is the one I would keep, and it is the
better-reasoned of the two:

- The row's payload is deliberately not scrubbed of one-time token material, so
  the retention window plus the purge command are the compensating control for
  retaining that material. Extending `expires_at` from an operator flag,
  repeatably (retry -> fail -> dead -> retry), removes that bound. That is a
  data-integrity argument and it is sound.
- The retry could not deliver anyway: the claim predicate is `expires_at > now`,
  so a re-opened expired row becomes `PENDING` and is never claimed - the old
  code could not send it either. Refusing does not reduce delivered mail; it
  stops the command from *claiming* to have re-queued something inert.

**Operator-account requirement ("an accurate account of every row asked about").
Enumerated the states and measured each** (`DeadRetryResult` = `requeued`,
`expired`, `expired_pks`; `DEAD_RETRY_PK_LOG_LIMIT = 20`):

| state | clause printed | distinguishable? |
|---|---|---|
| retried, will fail again | `re-opened 3 dead row(s), 0 left dead past their expiry` | yes |
| all refused as expired | `re-opened 0 dead row(s), 4 left dead past their expiry` + pks | yes |
| mixed | `re-opened 1 dead row(s), 2 left dead past their expiry` + pks | yes |
| retry matched nothing | `re-opened 0 dead row(s), 0 left dead past their expiry` | yes - clause still present |
| refusal list over the limit | `..., 23 left dead past their expiry` + `[1..20, (+3 more)]` | yes |

A refused row is **not** distinguishable from a row that was never dead - by
design they are the same thing, and both are counted under `dead` in the backlog,
which is the correct answer. An operator **can** tell "retried and failed again"
(`re-opened N`, `dead-lettered M` this pass) from "refused, will never be
retried" (`N left dead past their expiry`, plus pks). The clause is keyed on the
**flag**, not the counts, and is printed under `--status-only` too - which is what
stops "matched nothing" from reading as an absent number.

### BUG-3 [P3] unvalidated numeric command argument - CLOSED, mutation-proven

Broke it by reverting `type=positive_int` to `type=int`:

    OutboxBatchSizeArgumentTests: FAILED (failures=2)
      test_a_zero_batch_size_is_refused_by_name
      test_a_negative_batch_size_is_refused_rather_than_silently_accepted

Restore hash identical. State enumeration of the new `positive_int` (all seven
inputs measured, not sampled): `5 -> 5`, `1 -> 1`, `0`/`-1` refused with the
option named, `abc`/`3.5`/`""` refused as non-integer. The refusal arrives as
`CommandError: Error: argument --batch-size: ...`, i.e. argparse names the
offending option and value - the operator can act on it.

### BUG-4 [P3] status-report pin strength - CLOSED, mutation-proven, and the old pin proven blind

This is the finding the builder was asked to prove by *inducing* the hazard.
I induced it independently. Injected into the grouped queryset, in the copy:

    .values("status").annotate(total=Count("pk"))
      -> .values("status", "created_at").annotate(total=Count("pk"))

**With the hazard injected and the NEW pin:**

    FAILED: test_the_report_sums_every_row_in_a_status_not_just_the_last_group
    AssertionError: {'pending': 1, 'sent': 1, 'failed': 1, 'dead': 1}
                     != {'pending': 3, 'sent': 2, 'failed': 4, 'dead': 1}

**With the hazard still injected and the OLD cycle-1 pin reconstructed
(one row per status, expecting one):**

    Ran 1 test - OK

That is the control that makes the finding real: the old fixture cannot see the
hazard, the new one does, and the difference is the pin's strength rather than any
change in product code. The builder's reported outcome reproduces exactly.

Cycle 1 established the product code is correct on the installed Django version
and verified the emitted grouping; that stays closed and is consistent with what
I saw - the mutation that would expose the bug is one the ORM does not currently
emit.

## The complement - what nobody named

**1. Did extending ownership to the alert module pull in anything it should not?**
No. See BUG-1: the `ops/alerts.py` delta is three hunks - the docstring line, the
constant, the new function. `_in_cooldown`, `_send`, the settings read and the
cache backend are not in the diff. `ALERT_RECIPIENTS` and
`ALERT_COOLDOWN_SECONDS` already existed in `config/settings.py:628,632`, so
**no new env keys and no `.env.example` change are required**, and the diff
contains none.

**2. The new import edge - circular-import hazard, either import order,
half-initialisation.** `ops.alerts` imports `common.notifications` at module
level (line 40); the new edge is function-local (`from ops import alerts` inside
`_alert_dead_letter`), which is the correct shape.

Measured both orders, then established that my probe could actually detect the
hazard it claims to exclude - because a clean result from a probe that cannot
fail is not evidence:

    real tree, ops.alerts imported first      -> OK, notifications fully built
    real tree, common.notifications first      -> OK, notifications fully built
    SYNTHETIC control, module-level back-edge  -> ImportError: cannot import name
                                                 'B_THING' from partially
                                                 initialized module 'b' (most
                                                 likely due to a circular import)

The synthetic control **fires**, so the clean result on the real tree is
meaningful. I also tried forcing a module-level `from ops import alerts` into
`common.notifications.py` via a `MetaPathFinder` installed **before**
`django.setup()`; that attempt did **not** fire, because `django.setup()`
pre-imports `common.notifications` (`after django.setup(): common.notifications
preimported = True`, `ops.alerts preimported = False`), so the real import order
is fixed and benign regardless. An earlier version of that probe was
contaminated for the same reason and I discarded its output rather than report
it - recorded here because a probe that cannot fail is exactly how a cycle check
gets reported clean.

Conclusion: **no cycle, no half-initialised module, and no failure mode at
interpreter start.** Because the edge is function-local, the module is only
reached at call time, long after both modules are fully initialised.

**3. The refusal's new terminal state in the command's own output.** Covered by
the state matrix under BUG-2. The one case that reads ambiguously ("retry
matched nothing" -> `re-opened 0 ... 0 left dead past their expiry`) is
deliberately kept visible by keying the clause on the flag.

**4. Coverage is a floor, not evidence.** Enumerated the states the delta
introduces and drove each:

- new alert type x {dead-letter transition 1, transition 2, cooldown suppressed,
  cooldown lapsed, no recipients configured, alert send raises} - all driven.
- new refusal path x {row expired, row not expired, mixed, refusal list at the
  limit, refusal list over the limit} - all driven.
- `positive_int` x {positive, zero, negative, non-numeric} - all driven.
- new import edge x {both import orders} - measured directly.
- cooldown x {in window, window lapsed, cache backend raises} - driven.

Line coverage is **100%** on all three touched modules with **0 partial
branches** in each:

    common\management\commands\drain_notification_outbox.py   39 stmts, 12 branches, 100.00%
    common\notifications.py                                   224 stmts, 58 branches, 100.00%
    ops\alerts.py                                             80 stmts, 24 branches, 100.00%

All 21 partial branches in the suite live in other modules (accounts, cart, ops
commands, orders, products, shipping) - **none in any delta file**.

## Convention / stub / hardcode scans

- Stub scan on the three touched modules + the new template for
  `TODO|FIXME|XXX|HACK|NotImplementedError|pragma: no cover`, bare `pass`
  bodies, `settings.TESTING`/`settings.DEBUG` branches and `'test' in ...`
  test-aware branches: **clean**.
- Hardcode scan over added lines: one literal, `ALERT_RECIPIENT =
  "outbox-oncall@example.com"`, in the **test** module, and
  `nobody@example.com` in a simulated SMTP failure. Both are `example.com`,
  RFC 2606 reserved and non-routable - obviously fake test fixtures, not
  production-shaped secrets, and not matching any gitleaks allowlist rule. Per the
  secret-scan judgment rule these are **P3 hygiene, routed as an allowlist
  candidate, not blocking**; no production-shaped secret found. No new env keys,
  so `.env.example` correctly untouched.
- `makemigrations --check --dry-run`: `No changes detected`, exit 0.
- `ruff check .`: `All checks passed!`, exit 0.
- `ruff format --check` on the four touched files: `4 files already formatted`,
  exit 0.

## Gates, run last, after the narrative

    backend\venv\Scripts\python.exe scripts\doc_claims.py --base 13ec4d7
    doc_claims: 59 claims in 2 file(s) - 0 error(s), 59 to verify
    bytes  backend/docs/changes.md: ok (4 control, CR 0, LF True)
    exit 0

Run against the branch point and **after** the narrative commit `6eece42`, so the
figures quoted are not stale. Every `test_*` identifier cited in the delta's
evidence file was resolved by content search: 14 in
`tests/test_notification_outbox_worker.py`, and the 6 PG-red names in their real
homes (`products/tests.py`, `orders/tests.py`,
`tests/test_e2e_concurrency.py`, `tests/test_correlation_ids.py`). **No cited
name is dangling.** The follow-up doc commit `6eece42` replaced two abbreviated
test names with prose descriptions - the correct fix for "a fragment is not a
citation", and it does not make the document cite or count itself.

## Open finding this cycle

**BUG-5 [P3] `tests/test_notification_outbox_worker.py` - the docstring of
`test_repeated_failures_alert_again_once_the_cooldown_lapses` says "Two dead
rows, two alerts" while its body queues three undeliverable rows
(`alertrepeatone`, `alertrepeattwo`, `alertrepeatthree`) and asserts
`1, 1, 2` mails across them.** The assertions and the changelog row are both
correct ("a test drives three dead rows"); only this docstring is wrong, and it
is self-contradicting against its own body. Introduced by this delta - the string
does not exist at `13ec4d7`. `doc_claims.py` does not catch it because the claim
scanner reads the docs tree, not test docstrings. Classification: prose
(P3); no behaviour, no figure, no operator-visible consequence.

## Floor line

    floor 1844->1912, cov 99.80%, 9322 stmts, SQLite, 1912 tests OK (xf 4), exit 0
    floor 1844->1912, cov 99.80%, 9322 stmts, PostgreSQL, 1912 tests
           FAILED (failures=6, errors=1, xf 4), exit 1 (PG-2b + PG-2e, both
           pre-existing and outside this delta)

Before-figure 1844 is the `## Baseline` pair in `spec-run-state.md`, measured by
the orchestrator at the parallel-build anchor `2ba05ed`. Both `xf` counts held at
4; coverage did not drop; statements rose 9139 -> 9322; `makemigrations --check`
clean. The ledger's `## Baseline` block was **not** edited by this audit - the
orchestrator owns it.
