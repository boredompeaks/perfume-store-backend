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
| Red-team / probe scripts in-repo | **no existing row** — prior `red_team_probe.py` / `loadtest.py` were scratch, gitignored and **purged on 2026-09-30**; they were never repo code | none | ledgered here |

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

## TIER 2 — async / worker triage (owner's external audit flagged this critical)

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