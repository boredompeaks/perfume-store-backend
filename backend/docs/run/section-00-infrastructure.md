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

## Tasks

| Task ID | Req | Pri | Scope | Status |
|---|---|---|---|---|
| PG-1 | Run the whole backend suite against **PostgreSQL**, not SQLite | P1 | infra only, **no product code** | PENDING — **FIRST. Deliberately split from the fixes it will surface.** Land the wiring, run the suite, and **report every failure verbatim without fixing any of them.** A bundle that both changes the gate and repairs what it finds cannot tell which failures were pre-existing, and the whole value of this task is the measurement. Expect real defects: every SQLite-silent schema assumption in the repo becomes visible at once. |
| PG-2..n | Fix whatever PG-1 finds | — | triage after measurement | PENDING, derived from PG-1's output. Severity-rated at triage. |
| RUN-1 | Load / stress harness that tests **logic**, owned by SPEC-2-09 | P2 | new | PENDING — **blocked on PG-1 landing.** Must target oversell under concurrency, double-spend, reservation races, lost updates and TOCTOU, and must **fail loudly** when a guard is absent. A harness that reports "all good" against a guard that was deleted is worse than no harness. Deterministic concurrency, not timing luck. |
| RED-1 | Red-team + probe scripts, **in-repo and tracked** | P2 | new | PENDING — **blocked on PG-1.** Written to **BREAK the logic**, not to demonstrate compliance. A probe that only proves the happy path is a demo and belongs nowhere near this repo. Every probe must be able to fail, must name the invariant it attacks, and must record a verdict even when it finds nothing. |