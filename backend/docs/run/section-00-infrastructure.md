# Run infrastructure — cross-section work (not spec requirements)

Tasks here are **not** deferred rows from any spec section. They are
cross-cutting work the orchestrator has been asked to add, or rows whose owner
section recorded them but never built them. They sit outside the section
queues because no single spec section owns them.

Owner mapping, for the avoidance of doubt:

| Work | Origin | Owner section | Status |
|---|---|---|---|
| Load / stress harness | row **SPEC-2-09**, recorded by compliance during S21 and deferred to S2, never built | **Section 2** (technology stack), 9 deferred rows otherwise closed | ledgered here |
| Postgres as the test database | **no existing row** — surfaced as a structural blind spot; S22 wired `psycopg` and a `DATABASE_URL` parser but the suite has only ever run on SQLite | **Section 22-adjacent** (closed) | ledgered here |
| Red-team / probe scripts in-repo | **no existing row** — prior red-team and load-test scratch scripts were gitignored and **purged on 2026-09-30**; they were never repo code | none | ledgered here |

## THE ORDERING CONSTRAINT — read before scheduling anything else

**PG-1 must land before the stress harness, and that is not a preference.**

SQLite serialises writes and does not implement row-level locking, so the
concurrency defects this repo cares most about **cannot manifest on it at
all**. A stress harness written against SQLite would be theatre: it would
exercise the code paths and prove nothing about the logic, which is precisely
the failure the owner is asking to avoid. The same applies to the red-team
scripts — an oversell, a double-spend, a lost-update and a TOCTOU are all
invisible on a single-writer engine.

Worse, and already paid for repeatedly: **SQLite lies about the schema.** It
ignores `varchar(n)` where Postgres raises `DataError`, which has been charged
as a finding at least three separate times (B02 P2 prod-portability, B06 P3,
and the false `DataError` claim B07d cycle 2 had to retract). Every one of
those was a false green produced by the test database, not by the code.

So the sequence is **PG-1 -> triage the damage -> then RUN-1 / RED-1**. Building
the harnesses first would mean building them against an oracle that has
already been caught lying seven times in this run.

## PG-1 RESULT — the measurement, and why it reorders everything below

`349cc1a`. No wiring was needed: `psycopg[binary]==3.3.6` already pinned and installed, and `settings.py` already honours `DATABASE_URL`. The builder ran PostgreSQL 17.11 in a container (prod parity with `docker-compose.yml`) and measured.

**SQLite: `OK (expected failures=4)`, 1769 tests, 100.00%. PostgreSQL: `FAILED (failures=6, errors=68, expected failures=4)`, 73 missed, 99.17%. 0 skipped, 0 xpass. 74 problems on PG, 0 on SQLite.**

| Bucket | Count |
|---|---|
| A. Genuine product defect | **68** (3 root causes) |
| B. Test-only assumption | **6** (4 methods) |
| C. Wiring artefact | **0** |

**THE FINDING THAT MATTERS MOST — a P0 SQLite hid completely.** `orders/views.py:1667` — `select_for_update().select_related("coupon")` over a **nullable** FK raises `FOR UPDATE cannot be applied to the nullable side of an outer join` on Postgres. **Payment verification 500s for every customer in production.** 42 tests, plus 25 more where admin bulk actions inherit the same `select_related` on nullable `user`/`coupon` from `list_display`, so every admin bulk operation is dead too.

**AND THE CONSEQUENCE FOR EVERY HARNESS BELOW: 67 tests short-circuit at that `FOR UPDATE`, including `OversellRaceTests` and `CouponRaceTests`.** Their assertions are **unmeasured, not passing**. Any performance or concurrency harness built right now would be measuring a suite that silently stops before it asserts. **Do not read "74 failures" as "the other 1695 are proven."**

**Second structural fact:** `TestCase` wraps each test in a transaction, so true multi-connection `READ COMMITTED` — where oversell and double-spend actually live — is **never exercised**. RUN-1 must therefore use `TransactionTestCase` with real threads and connections. This is precisely why SQLite could never have found any of this.

**Third: the xfail ceiling is blind to this class.** `expected failures=4` is *identical* on both engines while its *content* differs — the slug-truncation test is decorated `expectedFailure` with a docstring predicting exactly this break, and in isolation reports `OK` in 0.016 s. **A count of expected failures cannot detect a database that changed underneath it.**

## TIER 0 — gate integrity and the P0. Nothing below is trustworthy until these land.

| Task | Req | Pri | Status |
|---|---|---|---|
| PG-2a | Fix the nullable-FK `select_for_update` — **payment verification 500s in production**, and every admin bulk operation with it | **P0** | PENDING — **FIRST, ahead of all section-1 work.** The `select_related` must move inside the lock or be dropped, and the 67 short-circuiting tests must be re-enabled, because they are the oversell and coupon-race coverage this repo is relying on. |
| PG-2b | Slug truncation `StringDataRightTruncation` on `varchar(100)` | P1 | PENDING — already predicted in an `expectedFailure` docstring; the prediction was right and nothing acted on it. |
| PG-2c | Close the xfail blind spot | P1 | PENDING — a decoration that reports OK while a database-dependent break hides behind it. |
| PG-2d | `load_dotenv()` + untracked `backend/.env` pointing at **production RDS** | P2 | PENDING — an unqualified `manage.py test` would create and drop a test database on production. Not triggered in this run; it is a loaded gun on the same trigger every developer uses. |

## TIER 1 — the harnesses. Blocked on Tier 0.

| Task | Req | Pri | Scope | Status |
|---|---|---|---|---|
| PERF-1 | **Performance harness**: p50/p95/**p99**, transaction time, logic-execution time, per-page load times for **storefront AND admin** | P2 | new | PENDING — blocked on PG-2a. **Runs locally during a train, never in CI** (perf in CI is noise, and a flaky gate is worse than no gate). **Must commit a recorded baseline artifact** or a p95 regression is invisible train-over-train — the same "no oracle for the claim" defect `doc_claims.py` exists to kill, applied to numbers. |
| RUN-1 | Load / concurrency harness that finds **what breaks first and why** (SPEC-2-09, owner S2) | P2 | new | PENDING — blocked on PG-2a. Must use `TransactionTestCase` + real threads/connections; deterministic contention, not timing luck. |
| RED-1 | Red-team + probe scripts, **in-repo and tracked** | P2 | new | PENDING — blocked on PG-2a. Written to **break** logic, never to demonstrate compliance. |

## TOOL-01 FOLLOW-UP — ruled a TOOL DEFECT, fix the scanner not the prose

The `doc_claims` scanner **cannot distinguish a `test_`-stemmed MODULE from a `test_` METHOD**, so any prose naming a test file trips a `test_name` check and demands a `def`. It fired **three times in one cycle**: once forcing a workaround in `changes.md`, twice in this ledger. Satisfying it cost **three real file identifiers** while two genuine bare-basename defects still had to be repaired by hand — the scanner could not tell those apart either.

**Auditor's ruling: a tool that punishes true prose and misses real defects is backwards, and rewordings are the wrong response** — that trains authors to write vaguer prose about real files, which is the exact failure the oracle exists to prevent. The fix is small and **strictly additive**: resolve a backticked `test_*` token against tracked files — if it is a file, classify it `[module_path]` and validate as a path; if not, keep today's `[test_name]` check. Under that rule all three workarounds become unnecessary. The oracle-conservatism counter (a false negative is worse than a false positive) was considered and does not survive **the tree being available**, because resolving the token is more informative, not less. **Land with the CI wiring.**

**Also pinned: `--base` convention.** A green `doc_claims` scan means "no errors across the diff from `<base>`", so a base several commits stale makes the scan **broader and therefore stronger**, never narrower — but a reader must not take a green scan as covering only the intended range. RE's CI must pass an explicit `--base`.

## PG-2a RESULT — SHIPPED

`1b4f1f1` (`orders/admin.py` 36/10 · `orders/views.py` 17/4 · a new row-locking guard module under `backend/tests/` 258/0).

**Both engines, full suite, measured by the builder:** SQLite **1776 / OK / xf 4 / 100.00% / 8818** (was 1769/8815) · PostgreSQL **1776 / `FAILED (failures=6, errors=1)` / xf 4 / 100.00% / 8818** (was `failures=6, errors=68`, 99.17%, 73 missed). **All 67 `NotSupportedError` gone, 67 -> 0**, measured by per-test exception map. `makemigrations --check` clean; Black 0 dirty added lines.

**PREMISE 1 WAS WRONG — `of=("self",)` was never needed.** Measured on SQLite: `has_select_for_update=False`, `for_update_after_from=False`, and the emitted SQL tail was plain `FROM "orders_order"` with **no `FOR UPDATE` and no exception**. **Django 6.1 silently DROPS the construct on SQLite**; it does not break. The chosen fix needs no backend capability because it emits **no outer join at all** — PG tail `... FROM "orders_order" ORDER BY id ASC FOR UPDATE` executes; SQLite is the same minus `FOR UPDATE`.

**PREMISE 2 WAS WRONG — admin does not "inherit `select_related` from `list_display`".** Neither `orders/admin.py` nor `common/admin.py` contains `select_related`, and `Order.objects.select_related()` with no arguments joins nothing. The real mechanism, read off captured SQL: **Django 6.1 `ChangeList.get_select_related_fields()` names the `list_display` FKs explicitly**, so `user` AND `coupon` both become LEFT OUTER JOINs under `select_for_update`.

**THE COUPON WAS ALREADY LOCKED, SEPARATELY.** `orders/views.py:1798-1800` read `order.coupon` only to take `.pk`, then **immediately re-fetched it under its own `Coupon.objects.select_for_update()`**, and every coupon field read (`active`/`valid_from`/`valid_until`/`usage_limit`/`used_count`) comes off the post-lock instance. So the join was a way to learn an FK id, not a locked read. Fix: read `order.coupon_id` off the already-locked Order row, keep the explicit coupon lock. **Strictly narrower — no new race.** Previously coupon columns were read at the instant of the order lock while unlocked; now every coupon field is read only after its own lock is held. Sole `Coupon` lock in the app, so no ABBA cycle is possible.

**THE FINDING THAT MATTERS MOST FOR RUN-1: `OversellRaceTests` and `CouponRaceTests` ARE SEQUENTIAL, NOT CONCURRENT.** Their own docstring says "in sequence — exactly the interleaving the row locks permit". They exercise the **post-lock sufficiency re-check**, not lock contention — and **on SQLite they would pass even with `select_for_update` deleted entirely.** The builder proved they are not vacuous by mutation (neutering the stock sufficiency re-check -> `CheckViolation` on `products_stock_check`; neutering the coupon validity re-check -> 200 != 409). **They were never weakened by this fix, and they never proved locking.** The repo's only "race" tests do not test races. **This is the strongest argument yet for RUN-1, and it is now evidence rather than intuition.**

**The third root cause is UNCHARACTERISED and has no task.** PG-1's 68 defects have **two** product root causes, not three. The remainder is **6 test-only Postgres failures** in two mechanisms: (a) **index introspection** — PG creates `varchar_pattern_ops` duplicates, so 4 tests assert a false "grew a non-unique duplicate index" invariant; (b) **sequences are NOT transactional**, so hardcoded PKs break — `data={"order_id": 1}` in the correlation-id test module (404 != 500) and `[611..615] != [1..5]` in `PaginationStabilityTests`. PG-2c is the xfail blind spot and PG-2d the `load_dotenv` hazard, so **nothing owns this.** Ledgered as **PG-2e**.

| Task | Req | Pri | Status |
|---|---|---|---|
| PG-2b | Slug `varchar(100)` `StringDataRightTruncation` | P1 | PENDING — 1 error still live on PG. |
| PG-2e | 6 test-only PG failures: `varchar_pattern_ops` index introspection + non-transactional sequences breaking hardcoded PKs | P1 | PENDING — **new, created by this task.** Test-side, but it means pagination and correlation-id tests are asserting engine-specific behaviour. |

## ASYNC-1 RESULT — the hypothesis is CONFIRMED, and it is worse than "slow"

**Confirmed with evidence.** One `send_mail` in the whole codebase (`common/notifications.py:47`, `fail_silently=False`); called inline from views, models and admin hooks; **no queue, no worker, no retry, no backoff, no dead-letter.** Not one `celery`/`rq`/`kombu` in `requirements.txt`, no `CACHES`, no scheduler, no cron service in `docker-compose.yml`.

**1. NO `EMAIL_TIMEOUT` IS SET (`config/settings.py:422-428`).** Django then passes `timeout=None` to `smtplib` — an **unbounded socket wait**. `.env.example` has no key either. This is not a slow p99; **a hung mail provider hangs the request forever**, while holding row locks.

**2. ZERO `transaction.on_commit` anywhere.** Every send happens **pre-commit, inside the caller's `atomic()`** — a cost the code itself documents. Worst: `orders/views.py:1943` (ORDER_PAID) inside payment verification's atomic, holding `select_for_update` on order, products and coupon.

**3. HIGHEST-RISK SITE — `orders/views.py:1943`.** The only send on a money-critical storefront path; it runs under row locks, pays the full SMTP handshake in the p99 tail of "payment successful", and its failure is the worst available: **the money has moved, the order is confirmed, and the confirmation email fails SILENTLY.** The customer sees a success page and waits forever for mail the logs call attempted and the system calls fine. With no timeout, that request **holds those row locks indefinitely and blocks every concurrent checkout touching the same SKUs.** Second: `products/models.py:189` — the same unbounded wait executed **once per opted-in user in a loop**, inside `adjust_stock`'s locked transaction.

**4. PER-PROCESS COOLDOWN.** `ops/alerts.py:22-23` admits `LocMemCache` ("Redis once SPEC-2-03 wires it") and **no `CACHES` is configured**, so the alert cooldown is **per-process** — gunicorn's workers each keep their own window, so the documented mail-bomb bound **does not hold across workers**.

**5. THE REGISTRY IS A HOOK INTO A VOID.** `_EVENT_HANDLERS` has exactly one entry, `ORDER_PAID`. `orders/events.py:43-45` dispatches `order.shipped` / `order.delivered` / `order.cancelled` as **bare strings that are not members of `AuditEvent.EventType`** and match no handler — DEBUG-logged no-ops at `notifications.py:106-107`. No receipt, no return/refund notification, no enquiry acknowledgement.

**6. ONE RECIPIENT'S FAILURE ABORTS THE REST.** `ops/alerts.py:107-111` wraps the whole recipient loop in one `try`, so a single SMTP failure abandons **every remaining recipient** and the partial failure is one swallowed exception.

**7. FOUR STALE CLAIMS FOUND** — including **two in this repo's own ledger**: `ops/alerts.py:6-7` says "four alert types" and defines **five**; `:20-22` claims a stock-edit alert trigger that **does not exist** (one caller, `ops/views.py:90`); and **`section-00-infrastructure.md` + `spec-run-state.md` describe `send_mail` as "scattered … in controllers" — STALE, there is now exactly ONE call site.** The R-19.0 deviation was remediated; the async defect in the same paragraph is real and unfixed.

| Task | Req | Pri | Status |
|---|---|---|---|
| ASYNC-2a | Set an SMTP timeout, env-driven, with a pin | **P1** | PENDING — smallest item in this queue and the cheapest risk reduction available: one settings key turns an unbounded hang into a bounded failure. |
| ASYNC-2b | Stop sending pre-commit inside `atomic()` — at minimum `ORDER_PAID` and the back-in-stock loop, which hold row locks across an unbounded network wait | **P1** | PENDING — **the highest-value item in the entire async queue.** A row lock held across an SMTP handshake is a checkout-blocking outage waiting for a bad minute. |
| ASYNC-2c | Jobs layer: outbox, retry with backoff, dead-letter, observability (**SPEC-2-03**) | P2 | PENDING — the structural fix. Depends on 2b establishing the outbox seam. |
| ASYNC-2d | Per-recipient failure isolation in the alert loop; shared cooldown backend | P2 | PENDING |
| ASYNC-2e | Register `shipped`/`delivered`/`cancelled` or stop dispatching them | P2 | PENDING — currently strings outside the `EventType` vocabulary, reaching nothing. |

**The external audit is right, and the root cause is one thing: notifications are SYNCHRONOUS.** `send_mail` is called inline from controllers; the ledger records R-19.0 (scattered manual `send_mail`) as a deviation owned by SPEC-2-03, and §19 never mandates Celery or Redis — so there is no queue, no retry, no backoff and no dead-letter anywhere.

That single fact shows up three ways, and it is the same defect wearing three coats:

1. **Performance.** SMTP latency sits on the critical path of the request, so an order confirmation inherits the full SMTP handshake in its tail. This is the most likely dominant term in p99 and PERF-1 must measure the email path specifically rather than treating it as background.
2. **Reliability — this is the "failure node".** If SMTP hangs, the order hangs. There is no queue to absorb it, no retry to ride out a transient, and nowhere for a poison message to die. A single unreachable mail provider is a store-wide order failure.
3. **Test fidelity.** A real background worker cannot be tested under `TestCase`'s transaction at all, so the async path is not merely slow, it is untestable as currently structured.

| Task | Req | Pri | Status |
|---|---|---|---|
| ASYNC-1 | Map **every** synchronous SMTP call on a request path, storefront and admin, with its blast radius | P1 | PENDING — measurement only, like PG-1: map, do not fix. |
| ASYNC-2 | Jobs layer: outbox, retry with backoff, dead-letter, observability (**SPEC-2-03**, unbuilt) | P2 | PENDING — depends on ASYNC-1's map. |

| Task ID | Req | Pri | Scope | Status |
|---|---|---|---|---|
| PG-1 | Run the whole backend suite against **PostgreSQL**, not SQLite | P1 | infra only, **no product code** | PENDING — **FIRST. Deliberately split from the fixes it will surface.** Land the wiring, run the suite, and **report every failure verbatim without fixing any of them.** A bundle that both changes the gate and repairs what it finds cannot tell which failures were pre-existing, and the whole value of this task is the measurement. Expect real defects: every SQLite-silent schema assumption in the repo becomes visible at once. |
| PG-2..n | Fix whatever PG-1 finds | — | triage after measurement | PENDING, derived from PG-1's output. Severity-rated at triage. |
| RUN-1 | Load / stress harness that tests **logic**, owned by SPEC-2-09 | P2 | new | PENDING — **blocked on PG-1 landing.** Must target oversell under concurrency, double-spend, reservation races, lost updates and TOCTOU, and must **fail loudly** when a guard is absent. A harness that reports "all good" against a guard that was deleted is worse than no harness. Deterministic concurrency, not timing luck. |
| RED-1 | Red-team + probe scripts, **in-repo and tracked** | P2 | new | PENDING — **blocked on PG-1.** Written to **BREAK the logic**, not to demonstrate compliance. A probe that only proves the happy path is a demo and belongs nowhere near this repo. Every probe must be able to fail, must name the invariant it attacks, and must record a verdict even when it finds nothing. |