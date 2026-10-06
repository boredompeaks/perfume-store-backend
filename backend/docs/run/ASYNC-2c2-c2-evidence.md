# ASYNC-2c2 — fix cycle 2 (audit cycle 1 verdict FIX)

Branch `feat/async-2c2`, HEAD under audit `13ec4d7`, delta `2ba05ed..13ec4d7`.
Cycle 1 classified itself `CYCLE ASYNC-2c2 -- CODE`. This cycle fixed the four
open findings and nothing else. Cycle 1's cleared axes were left alone: the
call-site conversion question, every queryset's lock/join shape, the envelope
shape, the summary's nominal ordering, the expected-failure ceiling's blindness,
and the changelog row's truthfulness.

Full logs for the two floor legs: `%TEMP%\opencode\preflight-sqlite.log`,
`preflight-pg.log`, `post-sqlite.log`, `post-pg.log`.

---

## Floor, both legs, re-derived in this session (never carried forward)

Pre-flight at `13ec4d7` with no changes applied — run to disambiguate
pre-existing red from red this cycle causes, and to reproduce rather than
transcribe the handed-in figures:

```
SQLite     Ran 1892 tests ... OK (expected failures=4)            exit 0
PostgreSQL Ran 1892 tests ... FAILED (failures=6, errors=1, expected failures=4)  exit 1
coverage   TOTAL 9294 stmts, 0 missed, 1352 branches, 21 partial, 99.80%  (both legs)
```

The seven PostgreSQL names, all pre-existing and none in `common/` or `ops/`:

```
ERROR: test_long_name_collision_truncated_to_slug_max_length        (products SlugGenerationTests)
FAIL:  test_order_number_stays_satisfied_by_its_unique_constraint
FAIL:  test_pages_partition_the_catalog_stably_across_repeated_requests
FAIL:  test_payment_provider_references_stay_constraint_covered
FAIL:  test_slug_stays_satisfied_by_its_unique_constraint
FAIL:  test_unhandled_exception_500_still_returns_the_request_id
```

(`test_payment_provider_references_stay_constraint_covered` appears twice in the
raw log via a subTest, so 6 unique names account for `failures=6, errors=1`.)

Final, after the fixes, printed by the gate itself —
`changelog_figures.py floor --task ASYNC-2c2-c2 --pg-url ...`, artifact
`scripts/figures/ASYNC-2c2-c2.json`:

```
- sqlite: Ran 1912 tests, OK (expected failures=4), cov 99.80% (9322 stmts, 0 missed, 1358 branches, 21 partial), test exit 0, coverage exit 0
- postgresql: Ran 1912 tests, FAILED (failures=6, errors=1, expected failures=4), cov 99.80% (9322 stmts, 0 missed, 1358 branches, 21 partial), test exit 1, coverage exit 0
```

1892 -> 1912 is this cycle's 20 new tests. `expected failures` held at 4.
Coverage percentage held at 99.80%; statements and branches rose with the new
code. `makemigrations --check --dry-run`: `No changes detected`, exit 0.
`ruff check .` exit 0, `ruff format --check .` exit 0.

---

## BUG-1 [P2] — the requirement "alerting on repeated failures" had no implementation

**Reproduced first.** The four new alert tests, against unmodified product
code, `manage.py test <the four new classes>`:

```
FAIL: test_a_row_that_can_never_be_resolved_raises_an_admin_alert          AssertionError: 0 != 1
FAIL: test_a_row_that_exhausts_its_attempts_raises_an_admin_alert          AssertionError: 0 != 1
FAIL: test_repeated_failures_alert_again_once_the_cooldown_lapses         AssertionError: 0 != 1
FAIL: test_an_alert_send_failure_never_breaks_the_drain_loop              AssertionError: 0 != 1
Ran 17 tests ... FAILED (failures=8)      (8 = these 4 + BUG-2's 2 + BUG-3's 2)
```

Each failure is the count of alert mails found where 1 was expected — zero
alert mail reached the configured recipient, exactly as the audit recorded.
These assert on MAIL, never on a log record, because a log line is what the
pre-fix code already emitted and asserting on one would pass against the defect.

**Fixed by:** `BACKGROUND_JOB_FAILURE = "background_job_failure"` and
`notify_background_job_failure(detail_text)` added to the alert module (which
was extended for this finding only), a template for it, and
`common.notifications._alert_dead_letter` called from BOTH dead-letter
transitions — `_record_failure` on the exhausted branch and `_dead_letter`.
The trigger sits beside the transition itself, the shape the security alert's
in-tree trigger site already uses.

**Cooldown: NOT TOUCHED.** `_in_cooldown`, `_send`'s use of it, the
`ALERT_COOLDOWN_SECONDS` read and the cache backend are all byte-identical to
HEAD. This cycle adds a caller and an alert type; the mechanism that bounds the
repeat belongs to another task and nothing here builds on top of it. The
"repeated" half of the requirement is the existing per-type cooldown doing its
existing job — `test_repeated_failures_alert_again_once_the_cooldown_lapses`
drives three dead rows and asserts one mail inside the window, a second after
it, so the reuse is observed rather than assumed.

The alert's detail carries the row pk, event type, attempt count and the same
bounded `_error_text` the row stores in `last_error`. `expires_at` is not
disclosed to the alert body.

---

## BUG-2 [P2] — the manual-retry path reported a success it could not deliver

**Reproduced first**, verbatim from the pre-fix run — the defect is fully
described by that one output line:

```
AssertionError: 're-opened 0 dead row' not found in
'drain_notification_outbox: examined 0; sent 0, failed 0, dead-lettered 0,
 vanished 0; re-opened 1 dead row(s); backlog is pending 1, sent 0, failed 0, dead 0.'
```

Re-opened one, became pending, zero mail sent, and no dead rows left to report.

### The recommendation, and the reasoning

**Chosen: refuse the retry for a row past its `expires_at`, leave it `DEAD`,
and report the refusal as its own number.**

Two obligations pull against each other and the resolution is a product call:

- *Extend the expired row's life so the retry can claim it.* Defensible on the
  operator's side: the retry is supposed to deliver, and this is the oldest
  failures an operator is working on. Rejected because the payload is
  deliberately NOT scrubbed of one-time token material — the accounts
  verification and reset mails are built out of exactly that — so deletion on a
  clock plus this expiry gate ARE the compensating control, as the substrate's
  own `serialize_context` docstring states. Extending `expires_at` would extend
  the retention of the most sensitive material in the table, from an operator
  flag, repeatably, with no bound: retry, fail, dead, retry.
- *Refuse it so the expiry keeps working.* Chosen. And it is not a consolation
  prize — the retry cannot deliver either way, because
  `PASSWORD_RESET_TIMEOUT` has almost certainly invalidated the token by now and
  a mail carrying a link that cannot work is worse than no mail. That is the
  same reason the claim predicate already refuses an expired row. So the choice
  is between violating the retention bound and sending a broken link, and only
  one of those two is available to the operator's own code.

If the product decides otherwise, the change is one filter clause and the
docstring paragraph that records this reasoning — the tests are written so that
flipping it is visible rather than silent.

**What the operator sees now**, and what the brief required: the command's
output and the function's docstring both say what happened to each row.

```
drain_notification_outbox: examined 1; sent 1, failed 0, dead-lettered 0, vanished 0;
re-opened 1 dead row(s), 2 left dead past their expiry; backlog is pending 0, sent 1,
failed 0, dead 2.
```

`retry_dead_notifications` returns a `DeadRetryResult(requeued, expired,
expired_pks)` instead of a bare int, so the two numbers cannot be conflated by
a caller. The observability signal survives because the refused rows STAY
`DEAD`: `outbox_status_counts` still reports them, their `last_error` still
says why they died, and `attempts` is not reset.

The retry clause in the summary is keyed on the FLAG, not on the counts, and it
is printed under `--status-only` too. Both choices are pinned by their own
tests, because the alternative reintroduces the finding's own shape: a clause
keyed on the counts goes silent on a `--retry-dead` that matched nothing, and
omitting it under `--status-only` means the one combination an operator runs to
*decide whether to retry* says nothing about the retry.

---

## BUG-3 [P3] — the batch-size argument was unvalidated

**Reproduced first:**

```
FAIL: test_a_zero_batch_size_is_refused_by_name                      CommandError not raised
FAIL: test_a_negative_batch_size_is_refused_rather_than_silently_accepted  CommandError not raised
```

Fixed with a `positive_int` argparse `type`, which names the offending option
and its value. `--batch-size 0`, `-3` and `many` all now raise `CommandError`;
`--batch-size 2` still stops the pass at 2 (regression-pinned by its own test).

---

## BUG-4 [P3] — the status-report pin could not fail on the hazard beside it

**Not a code defect** — the audit established the product code is correct on
the installed Django 6.1.1, and this cycle confirms it: the fold does not happen
as shipped. This was a pin-strength finding, so it was closed by inducing the
hazard, not by asserting it.

**The hazard, induced.** Adding one `.order_by("created_at")` to the grouped
queryset is exactly the fold — measured on the real queryset:

```
as shipped:  ... GROUP BY 1
induced:     ... GROUP BY 1, "common_notificationoutbox"."created_at" ORDER BY ... ASC
```

**The new pin, with the hazard in place:**

```
FAIL: test_the_report_sums_every_row_in_a_status_not_just_the_last_group
AssertionError: {'pending': 1, 'sent': 1, 'failed': 1, 'dead': 1}
            != {'pending': 3, 'sent': 2, 'failed': 4, 'dead': 1}
```

Last-write-wins under-reporting, exactly as predicted.

**The old pin, with the SAME hazard in place:**

```
Ran 3 tests in 0.016s        (green — blind to it)
```

That is the whole finding: one row per status merges nothing, so the hazard
that merges is invisible to a fixture that holds nothing to merge. The new
class gives every row its own distinct `created_at` — which is what makes the
fold observable — against hand-written literals, never a table derived from
the constant under test. The old single-row case is kept as its own test so
weakening back to it is a visible edit.

**Restored byte-identically.** After the induction the edit was reverted through
the editor and `git diff --stat -- backend/common/notifications.py` returned
empty, i.e. the file was byte-identical to `HEAD`.

---

## New states introduced, and how each was driven

Coverage measures lines executed, so the states this change adds were
enumerated and each driven rather than left to a percentage:

| New state | Driven by |
| --- | --- |
| alert on the exhausted-attempts path | `test_a_row_that_exhausts_its_attempts_raises_an_admin_alert` |
| alert on the unresolvable path | `test_a_row_that_can_never_be_resolved_raises_an_admin_alert` |
| alert suppressed by an unconfigured recipient list | `test_with_no_recipient_configured_...` |
| alert send failure swallowed, loop survives | `test_an_alert_send_failure_never_breaks_the_drain_loop` |
| retry re-opens an unexpired dead row | `test_a_row_whose_expiry_has_not_arrived_still_retries` |
| retry refuses an expired one and names it | `test_a_dead_row_past_its_expiry_stays_dead_and_is_named_in_the_report` |
| both halves of a mixed batch, both counts printed | `test_the_report_counts_the_refusals_next_to_the_rows_that_did_retry` |
| the refusal does NOT extend the expiry | `test_the_retry_never_moves_a_row_past_the_instant_it_expires` |
| refusal list longer than the log bound, marker honest | `test_a_long_refusal_list_is_bounded_in_the_log_but_never_in_the_count` |
| clause present under `--status-only`, absent without the flag | `test_status_only_still_reports_a_retry_that_ran`, `test_status_only_without_the_retry_flag_...` |
| `positive_int`: non-integer, below one, valid | three `OutboxBatchSizeArgumentTests` |
| grouped report summing many rows per status | `test_the_report_sums_every_row_in_a_status_not_just_the_last_group` |

`coverage report` on the three touched modules, after the fixes
(`--include` those modules, run over the two new test classes plus
`tests.test_alerts`):

```
common/management/commands/drain_notification_outbox.py   39 stmts   0 miss   12 branch   0 partial  100.00%
common/notifications.py                                   224 stmts  27 miss   58 branch   5 partial
ops/alerts.py                                              80 stmts   1 miss   24 branch   1 partial
```

The `common/notifications.py` and `ops/alerts.py` misses are all pre-existing
lines owned by other test modules (`dispatch`, `_describe`, `serialize_context`
and `notify_integration_outage`'s no-recipient branch); the final full-suite run
reports `0 missed` overall.

One thing this cycle changed on its own initiative: the first version of the
bounded-pk log appended the marker to the last pk (`..., 20 (+3 more)`), which
reads as though `20` were annotated. The marker is now its own
comma-separated element and the test asserts the shape.

---

## A pre-existing flake, reported rather than absorbed

One module run produced a single failure that did not recur on the next four
runs. Chased down rather than accepted:

```
with this cycle's changes:   test_two_workers_racing_one_row_claim_it_exactly_once
                             failed 2 of 8 consecutive runs
with all changes stashed at HEAD 13ec4d7:  the SAME test, 1 of 8 runs
```

Pre-existing, not caused here, in a two-thread claim race this cycle does not
touch. Not fixed — fixing it would be scope creep against a finding that was
not raised, and it is recorded so the next cycle does not spend budget
rediscovering it.

---

## Not re-read in this cycle, and why

Cycle 1's report and raw logs (outside the repo, not needed). The call-site
conversion question, every queryset's lock/join shape, the envelope shape, the
summary's nominal ordering, the expected-failure ceiling's blindness, and
cycle 1's changelog row — all cleared by cycle 1 and untouched here. The
conventions bible was read in full because it governs every edit in this cycle.
