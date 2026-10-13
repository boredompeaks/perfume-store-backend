# TIER1-GUEST-DEADEND — builder evidence

Task: refuse guest checkout explicitly instead of creating unpayable dead-end
orders that hold stock (owner decision 2026-10-09, `TIER1-launch.md`:
guest checkout DEFERRED, SPEC-3-02 off the Tier-1 list).

## Environment

- Worktree: `C:\Users\Admin\AppData\Local\Temp\opencode\spec-comp-wt`, branch
  `lane/a`, base `f3bb282`.
- PostgreSQL: container `pg2a`, port 5432, database `perfume_store`,
  user `perfume` — read from `docker exec pg2a env`
  (`POSTGRES_DB=perfume_store`, `POSTGRES_USER=perfume`) because the ledger's
  db-name note is known to mismatch; the container was trusted.
- `DATABASE_URL` passed as a process env var for each PG command only
  (never persisted): `postgres://perfume:pg2a_local_test_only@localhost:5432/perfume_store`.

## Commands and results (all re-derived in this session)

| When | Command | Result |
|---|---|---|
| Pre-flight (before any edit) | `coverage run manage.py test --verbosity 0` (PG) | Ran 2044 tests, OK (expected failures=3), 414.8s |
| Pre-flight coverage | `coverage report` | TOTAL 10185 stmts, 5 miss, cov 99.78% |
| New guest suite | `manage.py test orders.tests_guest` (PG) | OK (44 tests at first rewrite; 53 after the count-restoring pins) |
| Targeted affected files | `manage.py test` over the four affected modules (`backend/orders/tests_guest.py`, `backend/orders/test_idempotency.py`, `backend/shipping/tests.py`, `backend/tests/test_e2e_purchase_flow.py`) (PG) | Ran 178 tests, OK (expected failures=1 — the pre-existing F-18 flip test) |
| Orders app | `manage.py test orders` (PG) | Ran 543 tests, OK |
| Full suite + migrations | `coverage run manage.py test` + `coverage report` + `makemigrations --check` (PG) | Ran 2044 tests, OK (expected failures=3); TOTAL 10152 stmts, 5 miss, cov 99.77%; `No changes detected` |
| Changelog floor artifact (pre-format) | `python ../scripts/changelog_figures.py floor --task TIER1-GUEST-DEADEND --pg-url ...` | sqlite + postgresql: Ran 2044 tests, OK (expected failures=3), cov 99.77% (10152 stmts, 5 missed, 1380 branches, 21 partial), both exits 0; artifact `scripts/figures/TIER1-GUEST-DEADEND.json` |
| ruff | `ruff check` + `ruff format --check` on the 8 changed code files | check exit 0; format: one line re-wrapped in `orders/tests_guest.py`, then exit 0 |
| Post-format guest suite | `manage.py test orders.tests_guest` (PG) | OK |
| Changelog floor artifact (final, post-format) | same `floor` command re-run | Identical figures on both engines (quoted above) |
| doc_claims | `doc_claims.py --base ece53b8 --head f3bb282` | 1 error — `backend/docs/spec-run-state.md:165` test-name cite — PRE-EXISTING at the handoff base (this task never touched that file; orchestrator/auditor territory) |
| doc_claims (worktree) | `doc_claims.py --base ece53b8` (post-edit) | Same single pre-existing error; the task's own claims all resolve; `changes.md` bytes ok (4 control, CR 0, LF True) |
| changelog_figures | `changelog_figures.py check --base f3bb282` | 1 row with figures, 0 figures without artifact, exit 0 |

## Floor vs the recorded merged-tree floor

- Recorded floor (release-engineer, `d032655`): 2044 OK, xf 3, cov 99.77%
  (10185 stmts) both engines.
- This task, final tree: 2044 OK, xf 3, cov 99.77% (10152 stmts) both engines.
- Pass count and expected-failure count unchanged. Missed-statement count
  unchanged at 5 (all pre-existing — the tracked `STOCK-UNAVAILABLE-PIN`
  item in `TIER1-launch.md`). The percentage measured 99.78% on the
  pre-change tree and 99.77% after: the only movement is the removal of
  ~33 fully-covered dead statements (the guest-creation machinery), which
  shrinks the denominator over the same 5 misses. Keeping that machinery
  would have parked those statements permanently uncovered and dropped the
  floor further.

## Blast radius (files beyond the named scope, each retarget mandatory)

The refusal changes behaviour that four other files pinned through anonymous
POSTs; each retarget is listed in the changelog row:
`backend/orders/tests.py` (anonymous half of the cart-and-owner test),
`backend/orders/test_idempotency.py` (anonymous keyed checkout),
`backend/shipping/tests.py` (pricing-parity + replay-agreement classes
re-pointed at account callers; the guest keyed-drift 409 becomes the account
SPEC-9-01 collapse-keeps-original-price contract),
`backend/tests/test_e2e_purchase_flow.py` (unverified-account anonymous
halves). One-line stale-prose fixes in `backend/orders/urls.py` and
`backend/orders/views/__init__.py`.

## Negative-space pins (tests_guest.py)

(a) uniform 403 + SPEC-9-03 envelope; (b) zero Order/OrderItem/
OrderStatusEvent/StockReservation/StockMovement/ORDER_CREATED audit rows;
(c) product stock captured before/after unchanged; (d) coupon `used_count`
unchanged and no order carries the coupon; (e) refusal byte-identical across
14 guest reasons incl. both mounts (compared as `res.content` bytes);
(f) authenticated checkout e2e (checkout 201 → intent → verify → confirmed/
captured, stock 10→8, coupon spent once, hold CONVERTED). Legacy pre-refusal
guest rows (ORM fixtures of the exact old mint shape) keep every read path
pinned: token lookup, staff surfaces, webhook confirm, email, refund seam,
cancel/release, account-scoped payment/read exclusions, two-owner and
guest-key DB constraints.
