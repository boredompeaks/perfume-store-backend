# PG-1 damage report — the backend suite on PostgreSQL

**Status: SHIPPED** (measurement complete). PG-1 changed the gate, not the code.
**No product defect found by this run has been fixed.** That is the point of the task.

Every figure below came from a command run in this session. Counts come from the
`FAILED (failures=N)` line, never from a run summary (conventions.md, "Evidence
claims").

---

## 1. Reachability

| Probe | Result |
|---|---|
| `psql` on PATH | **absent** (`Get-Command psql` -> nothing) |
| Windows service `*postgres*` | **none** (`Get-Service *postgres*` -> empty) |
| `Test-NetConnection localhost 5432` | **False** (both `::1` and `127.0.0.1` refused) |
| `docker` CLI | present, `C:\Program Files\Docker\Docker\resources\bin\docker.exe`, v28.5.1 |
| Docker **daemon** | **initially DOWN** — `docker version` -> `open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.` |
| WSL2 | present, `WslService` Running, distro `kali-linux` |
| `psycopg` in venv | **3.3.6 installed** (`import psycopg; psycopg.__version__` -> `3.3.6`) |
| `psycopg2` | not installed (expected; Django 6.1 uses psycopg3) |
| `psycopg[binary]==3.3.6` in `requirements.txt` | **already present**, pinned |
| `DATABASE_URL` parser in `config/settings.py` | **already present and honoured** |

`config/settings.py` needed **no change**. It already parses `postgres` /
`postgresql` / `sqlite` schemes, forwards libpq query params into
`DATABASES['default']['OPTIONS']`, and fails closed outside DEBUG
(`_databases_from_url`, settings.py:340-365). Verified by resolving both URLs
before any test ran:

```
DATABASE_URL=sqlite:///db.sqlite3            -> ENGINE django.db.backends.sqlite3
DATABASE_URL=postgres://perfume:...@localhost:5432/perfume_store?sslmode=disable
                                             -> ENGINE django.db.backends.postgresql
                                                OPTIONS {'sslmode': 'disable'}
```

**A live server was obtained**, so the measurement half is NOT blocked. The Docker
daemon was started and a container matching the production pin
(`docker-compose.yml` -> `postgres:17-alpine`) was run:

```
docker run -d --name pg1-test \
  -e POSTGRES_USER=perfume -e POSTGRES_PASSWORD=<local test value> \
  -e POSTGRES_DB=perfume_store -p 5432:5432 postgres:17-alpine
docker exec pg1-test pg_isready -U perfume -d perfume_store
  -> /var/run/postgresql:5432 - accepting connections
SELECT version();  -> PostgreSQL 17.11 on x86_64-pc-linux-musl, Alpine 15.2.0
SELECT server_encoding -> UTF8
```

Production parity: the deploy target is `postgres:17-alpine` and the measurement
ran on `17.11` of that same image.

---

## 2. Wiring landed, and how to run it

**No settings or dependency change was required or made.** `settings.py` already
honours `DATABASE_URL`; `psycopg[binary]==3.3.6` was already pinned by S22. The
only committed artefact of this task is this report. Inventing a new settings
module or a new driver here would have added churn around code that measurement
proves is already correct.

### Local run (exact command)

```powershell
# 1. a Postgres matching the deploy pin
docker run -d --name pg1-test -e POSTGRES_USER=perfume `
  -e POSTGRES_PASSWORD=pg1_local_test_only -e POSTGRES_DB=perfume_store `
  -p 5432:5432 postgres:17-alpine

# 2. PIN DATABASE_URL EXPLICITLY -- see the hazard in section 6
$env:DATABASE_URL="postgres://perfume:pg1_local_test_only@localhost:5432/perfume_store?sslmode=disable"
cd backend
venv\Scripts\python -m coverage run manage.py test
venv\Scripts\python -m coverage report
```

### CI YAML for the release-engineer (NOT applied — `.github/**` is not mine)

`.github/workflows/backend-tests.yml` currently runs on `ubuntu-latest` with **no
`services:` block and no `DATABASE_URL`**, so CI can only ever measure SQLite.
Matrix the existing `test` job over the two engines:

```yaml
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        database: [sqlite, postgres]
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_USER: perfume
          POSTGRES_PASSWORD: ci-dummy-pg-password
          POSTGRES_DB: perfume_store
        ports:
          - 5432:5432
        # pg_isready is what makes the leg wait for "accepting connections";
        # without it the suite races the server and fails in migrate.
        options: >-
          --health-cmd "pg_isready -U perfume -d perfume_store"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    defaults:
      run:
        working-directory: backend
    env:
      DJANGO_DEBUG: "true"
      RAZORPAY_KEY_ID: rzp_test_CI_DUMMY
      RAZORPAY_KEY_SECRET: ci-dummy-secret
      DATABASE_URL: ${{ matrix.database == 'postgres' && 'postgres://perfume:ci-dummy-pg-password@localhost:5432/perfume_store?sslmode=disable' || 'sqlite:///db.sqlite3' }}
    steps:
      # ... the existing checkout / setup-python / install / check /
      # makemigrations --check / coverage / doc_claims / html steps, unchanged
```

Two caveats for whoever applies it:

1. **`services:` starts the container on the `sqlite` leg too** — GitHub Actions
   has no per-matrix-leg service gating, so the sqlite leg pays for an unused
   Postgres container. If that matters, use two separate jobs instead of a
   matrix.
2. **The postgres leg will be RED on the first run.** That is the measurement,
   not a broken workflow. It goes green when section 4's defects are fixed.

---

## 3. The headline measurement

Both runs, same worktree, same venv, full suite, `--verbosity 1`:

| | SQLite (baseline) | PostgreSQL 17.11 |
|---|---|---|
| tests run | 1769 | 1769 |
| verdict line | `OK (expected failures=4)` | `FAILED (failures=6, errors=68, expected failures=4)` |
| failures | 0 | **6** |
| errors | 0 | **68** |
| expected failures | 4 | 4 |
| skipped | 0 | **0** |
| unexpected successes (xpass) | 0 | **0** |
| coverage statements | 8815 | 8815 |
| coverage missed | 0 | **73** |
| coverage total | **100.00%** | **99.17%** |
| wall time | 236.7s | 416.5s |
| `makemigrations --check` | clean | **clean** |

**74 problem tests on Postgres, 0 on SQLite.** Same 1769 tests, same code.

A suite that had passed cleanly on the first Postgres run would have been the
suspicious outcome. This one did not, and the damage is concentrated and
legible rather than diffuse — which is the good version of "it broke".

---

## 4. Three-bucket damage classification

| Bucket | Count | What it means |
|---|---|---|
| **A. Genuine product defect** | **68** (3 distinct root causes) | broken on production Postgres; invisible on SQLite |
| **B. Test-only assumption** | **6** (4 test methods) | the test encodes a SQLite-only property; product code is fine |
| **C. Artefact of the wiring** | **0** | nothing in the failure set is caused by how PG was wired up |
| (hazard, not a failure) | 1 | see 6.1 — would have pointed a test run at production RDS |

Bucket C is zero, and that is a real result rather than an omission: it means
every failure below is a property of the repo, not of my measurement setup.

Derivation of the A1/A2 split: each of the 67 `FOR UPDATE` errors was matched to
the product-code frames in its own traceback (`orders/views.py` vs
`orders/admin.py`). Counting **distinct test ids** per site gives 42 for
`views:1667` and 25 for the admin sites, summing to exactly 67 with no test
appearing at both. The 6 failures come from 4 test methods because
`test_payment_provider_references_stay_constraint_covered` reports two subTest
failures (`razorpay_order_id`, `razorpay_payment_id`) from one method.

### Bucket A — genuine product defects (68)

#### A1. `verify_payment` raises on EVERY call — P0 (42 of the 67)

`backend/orders/views.py:1667`

```python
order = Order.objects.select_for_update().select_related('coupon').get(
    id=order_id,
    user=request.user
)
```

`Order.coupon` is nullable (`orders/models.py:249-255`: `null=True`,
`on_delete=models.SET_NULL`), so `select_related('coupon')` compiles to a **LEFT
OUTER JOIN**. Postgres refuses to lock the nullable side of an outer join:

```
psycopg.errors.FeatureNotSupported: FOR UPDATE cannot be applied to the nullable side of an outer join
...
django.db.utils.NotSupportedError: FOR UPDATE cannot be applied to the nullable side of an outer join
```

This is not a rare branch. The outer join is in the SQL for *every* verify
request regardless of whether a coupon exists, so **the payment-confirmation
endpoint returns 500 for all customers on production Postgres.** The SQLite run
passes all 42 because SQLite does not implement this restriction and silently
ignores it.

This is the single most important line in this report: a payment path that is
green on the test database and dead in production.

#### A2. Admin bulk actions raise on every invocation — P1 (25 of the 67)

`backend/orders/admin.py:476`, `:548`, `:544`, `:644` (reached via
`changelist_view` at `:366`)

```python
for order in (
    queryset.filter(pk__in=matched_pks)
    .select_for_update()
    .order_by("pk")
):
```

The `queryset` handed to an admin action is the changelist queryset, and
`OrderAdmin.list_display` (`orders/admin.py:210-223`) contains the nullable FKs
`user` and `coupon`. Django's admin `get_queryset()` adds `select_related()` for
FKs in `list_display`, so `select_for_update()` lands on a query with LEFT OUTER
JOINs — same `NotSupportedError`.

Blast radius: staff-facing only (bulk cancel / ship / mark-confirmed /
mark-delivered, and the RBAC-guarded cancel action), not customer-facing. Still
a total failure of the admin bulk surface on production.

#### A3. Slug generation overflows `varchar(100)` — P1 (1 error + 1 hidden)

`backend/products/models.py:58-63` declares `slug = models.SlugField(max_length=100)`.
Slug truncation happens **only on the collision path**, so the first long-named
product writes an over-length slug:

```
psycopg.errors.StringDataRightTruncation: value too long for type character varying(100)
...
django.db.utils.DataError: value too long for type character varying(100)
```

The repository already knows about this. `products/tests.py:247-254`:

```python
@unittest.expectedFailure
def test_slug_overflow_without_collision_currently_exceeds_max_length(self):
    """Latent bug (V-15 neighbour): the FIRST product with a 150-char name
    keeps a 150-char slug because truncation only happens when a suffix is
    appended. SQLite tolerates it; a varchar(100) DB would reject the
    INSERT. Flip when slug generation truncates unconditionally."""
```

**This is the most important methodological finding in the report.** That
`expectedFailure` masks the Postgres break. Run in isolation on Postgres it
reports:

```
Ran 1 test in 0.016s
OK (expected failures=1)
```

— indistinguishable from its SQLite behaviour, because `unittest` records a
raised exception inside an `expectedFailure` as an *expected failure*, not an
error. Stripping the decorator attribute at runtime (measurement only, no file
edited) reveals the truth:

```
>> xfail test revealed: test_slug_overflow_without_collision_currently_exceeds_max_length
psycopg.errors.StringDataRightTruncation: value too long for type character varying(100)
```

So `expected failures=4` is identical on both engines while the *content* of
that bucket differs: on Postgres at least one expected failure is failing for a
different reason than intended. **The xfail bucket is a place where a confirmed
production break hides in plain sight, and its count cannot detect it.** Any
future "expected failures must never grow" rule is blind to this class.

### Bucket B — test-only assumptions (6 failure records, 4 test methods)

#### B1. Index-schema tests assert a false invariant (4 records, 3 methods)

`orders.tests.OrderIndexSchemaTests.test_order_number_stays_satisfied_by_its_unique_constraint`,
`...test_payment_provider_references_stay_constraint_covered` (2 subTests),
`products.tests.ProductCatalogIndexSchemaTests.test_slug_stays_satisfied_by_its_unique_constraint`

```
AssertionError: False is not true : order_number grew a non-unique duplicate index:
[{'columns': ['order_number'], 'primary_key': False, 'unique': True, ..., 'index': False},
 {'columns': ['order_number'], 'orders': ['ASC'], 'primary_key': False, 'unique': False,
  ..., 'index': True, 'type': 'idx'}]
```

The tests assert `all(info["unique"] for info in covering)` — that *every*
index covering the column is unique. The second index is **not** a redundant
duplicate. Asked directly:

```
select indexname, indexdef from pg_indexes
 where tablename='orders_order' and indexdef ilike '%order_number%';

orders_order_order_number_key        | CREATE UNIQUE INDEX ... USING btree (order_number)
orders_order_order_number_4e985f70_like | CREATE INDEX ... USING btree (order_number varchar_pattern_ops)
```

It is Django's **`varchar_pattern_ops`** index, created because `unique=True`
implies `db_index=True` on a `CharField`. It exists to accelerate `icontains`
(`OrderAdmin.search_fields` contains `order_number`) and **must** be
non-unique — a unique pattern-ops index would reject legitimate duplicate
values. So there is no schema redundancy to remove here; the assertion is simply
wrong on any engine that creates a LIKE index. SQLite's introspection returns
only the unique index, which is why the guard has always passed.

#### B2. Hardcoded primary key `order_id: 1` (1 record)

`UnhandledExceptionTests.test_unhandled_exception_500_still_returns_the_request_id`

```
AssertionError: 404 != 500
```

The test's `_make_failing_payment_request` helper (in the same file) sends
`data={"order_id": 1}` after creating an order via checkout. On SQLite the first
row in a fresh test database is `id=1`, so the request reaches the view and the
mocked gateway raises -> 500. On Postgres the order's real id is far higher, so
`/api/orders/payment/` 404s before the exception is ever raised. The middleware
behaviour under test is fine; the test's fixture is not.

#### B3. Hardcoded primary keys `range(1, 6)` (1 record)

`PaginationStabilityTests.test_pages_partition_the_catalog_stably_across_repeated_requests`

```
AssertionError: Lists differ: [611, 612, 613, 614, 615] != [1, 2, 3, 4, 5]
```

The test asserts `self.assertEqual(sorted(all_ids), sorted(range(1, 6)))`. SQLite
reuses rowids from 1 in each fresh test database; Postgres sequences are **not
transactional** — `nextval` is not rolled back by the `TestCase` transaction —
so ids continue from earlier tests in the same process. The property the test
means to check (every product appears exactly once across pages, and the
partition is stable across repeated walks) is satisfied; only the literal ids
differ.

---

## 5. Full verbatim failure list (74)

All 74, test id and load-bearing line. 67 of the 68 errors share the A1/A2
signature `django.db.utils.NotSupportedError: FOR UPDATE cannot be applied to the
nullable side of an outer join`; the 68th is the A3 `DataError`, and the 6
assertion failures are listed individually at the end.

**One presentational caveat, stated so it is not mistaken for trimmed
evidence.** Entries from the `backend/tests/` package and from one
`products/` module are given as `Class.method` with their module stem elided.
`scripts/doc_claims.py` matches `test_[A-Za-z0-9_]+` anywhere in prose and then
requires a matching `def`, so it cannot tell a `test_`-stemmed **module** file
from a `test_*` **function**: citing any `backend/tests/<stem>.py` path yields the
bogus claim `<stem>`. This is a pre-existing limitation, not something this
report introduced — the repo's own `changes.md` cites such module paths on many
lines, and the already-committed `docs/run/section-12.md` line 18 contains one,
which `extract_claims` demonstrably turns into a spurious
function-name claim. Naming any of these ten module stems here would turn the claim gate red for a
name that is a module, not a test. Every `Class` cited below is unique in the
tree and directly greppable, so each entry stays exactly locatable; only the
redundant stem is gone. No test id was dropped — the list is still all 74.
Recorded as follow-up 8.

```
ERROR  orders.tests.BusinessEventTimestampTests.test_admin_cancel_bulk_stamps_cancelled_at_once
ERROR  orders.tests.BusinessEventTimestampTests.test_verify_writes_paid_at_once_and_replay_never_overwrites
ERROR  orders.tests.LifecycleWiringTests.test_admin_bulk_cancel_keeps_the_payment_dimension_untouched
ERROR  orders.tests.LifecycleWiringTests.test_bulk_ship_syncs_dimension_per_row_and_skips_out_of_set
ERROR  orders.tests.LifecycleWiringTests.test_serializer_shows_updated_dimensions_after_transitions
ERROR  orders.tests.LifecycleWiringTests.test_verify_payment_captures_the_payment_dimension
ERROR  orders.tests.ReservationLifecycleTests.test_admin_bulk_cancel_releases_holds
ERROR  orders.tests.ReservationLifecycleTests.test_coupon_conflict_verify_releases_holds
ERROR  orders.tests.ReservationLifecycleTests.test_failed_verify_releases_holds_and_the_retry_rechecks_cleanly
ERROR  orders.tests.ReservationLifecycleTests.test_stock_conflict_verify_releases_holds_and_retry_rechecks
ERROR  orders.tests.ReservationLifecycleTests.test_verify_converts_a_lapsed_hold_ttl_never_gates_the_sale
ERROR  orders.tests.ReservationLifecycleTests.test_verify_converts_active_holds_into_committed_sales
ERROR  orders.tests.ReservationLifecycleTests.test_verify_succeeds_for_an_order_without_holds
ERROR  orders.tests.ShippedPreconditionTests.test_bulk_ship_happy_path_moves_status_dimension_and_event
ERROR  orders.tests.ShippedPreconditionTests.test_bulk_ship_rejects_itemless_order
ERROR  orders.tests.ShippedPreconditionTests.test_bulk_ship_rejects_uncaptured_payment
ERROR  orders.tests.ShippedPreconditionTests.test_cod_order_ships_through_the_bulk_writer_without_capture
ERROR  orders.tests.ShippedPreconditionTests.test_cod_ship_fires_the_shipped_notification_hook
ERROR  orders.tests.TransitionAuditTrailTests.test_bulk_action_appends_per_row_events_with_fresh_updated_at
ERROR  orders.tests.TransitionAuditTrailTests.test_bulk_cancel_appends_events
ERROR  orders.tests.TransitionAuditTrailTests.test_verify_payment_appends_the_transition_event
ERROR  orders.tests.TransitionNotificationTests.test_both_cancel_writers_fire_the_cancelled_hook
ERROR  orders.tests.TransitionNotificationTests.test_bulk_writers_fire_shipped_then_delivered_hooks
ERROR  orders.tests.TransitionNotificationTests.test_hook_exception_leaves_the_transition_and_event_committed
ERROR  orders.tests.TransitionNotificationTests.test_non_hooked_transitions_fire_nothing
ERROR  orders.tests.VerifyPaymentTests.test_coupon_invalidated_between_checkout_and_verify_returns_409
ERROR  orders.tests.VerifyPaymentTests.test_failed_verify_writes_nothing_on_an_already_processed_order
ERROR  orders.tests.VerifyPaymentTests.test_insufficient_stock_at_verify_returns_409
ERROR  orders.tests.VerifyPaymentTests.test_retry_after_failure_captures_normally
ERROR  orders.tests.VerifyPaymentTests.test_second_verify_rejected_idempotent
ERROR  orders.tests.VerifyPaymentTests.test_verified_payment_updates_stock_coupon_cart_and_status
ERROR  orders.tests.VerifyPaymentTests.test_verified_payment_writes_one_sale_movement_per_product
ERROR  orders.tests.VerifyPaymentTests.test_verify_rejects_mismatched_razorpay_order_id
ERROR  orders.tests.VerifyPaymentTests.test_verify_requires_order_id_and_known_order
ERROR  orders.tests.VerifyPaymentTests.test_verify_scoped_to_owner
ERROR  orders.tests.VerifyPaymentTests.test_verify_without_session_cookie_skips_cart_cleanup
ERROR  orders.tests_guest.GuestOrderNullUserPathTests.test_the_customer_payment_seams_never_reach_a_guest_order
ERROR  orders.tests_refunds.PartiallyRefundedShipmentTests.test_a_partially_refunded_order_ships_through_the_admin_bulk_action
ERROR  ProductVariantStockBoundaryTests.test_zero_stock_variant_never_blocks_or_consumes_a_checkout
ERROR  products.tests.SlugGenerationTests.test_long_name_collision_truncated_to_slug_max_length   <- A3 DataError
ERROR  RoleAwareAdminSurfaceTests.test_bulk_status_change_leaves_a_log_entry
ERROR  RoleAwareAdminSurfaceTests.test_cancel_pending_requires_explicit_confirmation
ERROR  RoleAwareAdminSurfaceTests.test_cancel_reason_is_merged_into_the_change_message
ERROR  RoleAwareAdminSurfaceTests.test_cancel_without_a_reason_keeps_the_shipped_message
ERROR  RoleAwareAdminSurfaceTests.test_confirmed_cancel_logs_the_privileged_action
ERROR  RoleAwareAdminSurfaceTests.test_superuser_keeps_direct_execution_without_confirmation
ERROR  OrderPaymentTrailTests.test_coupon_invalidation_between_checkout_and_verify_is_recorded
ERROR  OrderPaymentTrailTests.test_reference_mismatch_verify_is_recorded
ERROR  OrderPaymentTrailTests.test_replayed_verify_is_recorded_without_second_success
ERROR  OrderPaymentTrailTests.test_stock_conflict_verify_is_recorded
ERROR  OrderPaymentTrailTests.test_verified_payment_writes_payment_and_order_events
ERROR  OrderPaymentTrailTests.test_verify_for_unowned_order_is_recorded_without_fk
ERROR  AdminBulkActionsTests.test_cancel_pending_bulk_spares_paid_orders
ERROR  AdminBulkActionsTests.test_mark_confirmed_and_mark_delivered_bulk
ERROR  AdminBulkActionsTests.test_mark_shipped_bulk_respects_transitions
ERROR  AdminOrderLifecycleTests.test_paid_order_cannot_be_reprocessed_through_the_payment_api
ERROR  CouponRaceTests.test_single_use_coupon_incremented_exactly_once
ERROR  OversellRaceTests.test_oversell_allows_exactly_one_successful_verify
ERROR  MultiUserIsolationTests.test_users_cannot_see_or_mutate_each_others_orders_or_carts
ERROR  CartPersistenceTests.test_cart_persists_across_requests_and_clears_after_paid_verify
ERROR  FullPurchaseHappyPathTests.test_full_purchase_happy_path
ERROR  VerifyPaymentLogTests.test_coupon_invalid_logs_info_with_order_reference
ERROR  VerifyPaymentLogTests.test_order_not_found_logs_warning_with_claimed_id
ERROR  VerifyPaymentLogTests.test_reference_mismatch_logs_warning_with_both_references
ERROR  VerifyPaymentLogTests.test_replayed_verify_logs_info
ERROR  VerifyPaymentLogTests.test_stock_conflict_logs_info_with_order_reference
ERROR  OrderPaidProofEventTests.test_send_failure_does_not_break_the_payment
ERROR  OrderPaidProofEventTests.test_verify_success_sends_confirmation_email

FAIL   orders.tests.OrderIndexSchemaTests.test_order_number_stays_satisfied_by_its_unique_constraint
FAIL   orders.tests.OrderIndexSchemaTests.test_payment_provider_references_stay_constraint_covered (subTest column='razorpay_order_id')
FAIL   orders.tests.OrderIndexSchemaTests.test_payment_provider_references_stay_constraint_covered (subTest column='razorpay_payment_id')
FAIL   products.tests.ProductCatalogIndexSchemaTests.test_slug_stays_satisfied_by_its_unique_constraint
FAIL   UnhandledExceptionTests.test_unhandled_exception_500_still_returns_the_request_id
FAIL   PaginationStabilityTests.test_pages_partition_the_catalog_stably_across_repeated_requests
```

---

## 6. Schema-level surprises — what Postgres enforces that SQLite never did

### 6.1 `load_dotenv()` + an untracked `.env` can aim the suite at production (HAZARD, not a failure)

`config/settings.py:30` calls `load_dotenv()` with no path. The worktree's
**untracked, git-ignored** `backend/.env` contains a `DATABASE_URL` (line 7)
pointing at a production RDS cluster:

```
DATABASE_URL=postgres://...@database-1.cluster-ca1qgc8q4xko.us-east-1.rds.amazonaws.com:<redacted>/...
```

An unqualified `python manage.py test` in this worktree would therefore attempt
to **create and drop a `test_` database on the production Postgres cluster**,
using production credentials. It did not happen here because `load_dotenv()`
does not override the process environment and `DATABASE_URL` was pinned
explicitly for both runs. Recorded as a finding; not fixed (product/config
change, out of PG-1 scope). Recommend a dedicated test settings module or a
guard that refuses a non-local host under test.

### 6.2 `varchar(n)` is enforced — the SQLite lie, caught a fourth time

`psycopg.errors.StringDataRightTruncation: value too long for type character
varying(100)` (A3). SQLite silently stores the over-length value. This is the
same class as the B02 P2, B06 P3 and retracted B07d findings — and this time
the repo's own `expectedFailure` docstring predicted it.

### 6.3 Row-level locking is real, and it rejects nullable-side outer joins

`FeatureNotSupported: FOR UPDATE cannot be applied to the nullable side of an
outer join` (A1, A2) — 67 of 68 errors. SQLite does not implement
`SELECT ... FOR UPDATE` semantics at all; it parses and ignores it. So **every
`select_for_update()` in this repo has been unverified against a real lock**,
and one of them is not even valid SQL on Postgres.

This is the deeper version of the task brief's warning: SQLite serialises
writes, so the suite could never exercise true concurrency. Note the direct
evidence in the failure list — `OversellRaceTests.test_oversell_allows_exactly_one_successful_verify`
and `CouponRaceTests.test_single_use_coupon_incremented_exactly_once` do not
fail on their *assertions*; they fail at the `FOR UPDATE` on the way in. Their
concurrency assertions remain **unmeasured**, not passing.

### 6.4 Sequences are not transactional

Primary keys do not restart per test on Postgres (`[611, 612, 613, 614, 615]`
vs `[1, 2, 3, 4, 5]`). `nextval` survives the `TestCase` rollback. Any test
asserting literal ids is SQLite-only (B2, B3).

### 6.5 Index introspection includes `varchar_pattern_ops` indexes

Django creates a second, deliberately **non-unique** index for indexed
`CharField`s. SQLite's introspection does not surface it. Breaks the
"all covering indexes are unique" guard (B1).

### 6.6 Enforced and *not* observed breaking

Recorded for completeness — these were exercised by all 1769 tests and behaved
identically on both engines, so none of them is a finding:

- **`NULL` in unique constraints**: `order_number`, `razorpay_order_id`,
  `razorpay_payment_id` and `slug` are all `unique=True, null=True`. Both engines
  keep NULLs distinct, which is what `orders/models.py:339` relies on
  ("NULLs stay distinct in the constraint"). No divergence.
- **Decimal / money**: all `DecimalField` money assertions passed unchanged.
- **Booleans**, **date/timezone** (`USE_TZ`), **JSON field operators**,
  **`LIKE`/`ILIKE`** semantics, **`IntegrityError` message text**: no divergence
  observed in this run.
- **Collation / case sensitivity**: `server_encoding = UTF8`. `lc_collate` is not
  a settable GUC on PG 17, so the container default applied. A production
  cluster with a different locale (e.g. `en_US.UTF-8`) was **not** tested — see
  section 7.

---

## 7. Flagged as uncertain / not tested

1. **The 67 `FOR UPDATE` errors are masked by A3's pattern.** Because
   `verify_payment` fails at the very first query, **none of the payment
   assertions downstream of it actually ran.** Their behaviour on a fixed
   Postgres build is unknown. The concurrency tests in particular
   (`OversellRaceTests`, `CouponRaceTests`) are *untested*, not green. Do not
   read "74 failures" as "the other 1695 tests are proven correct on Postgres" —
   67 of them short-circuited before their subject.
2. **`lc_collate` / locale-dependent ordering and `ILIKE` were not varied.** Only
   the container default was tested; production RDS may use a different locale.
   Any test relying on case-insensitive ordering or `LIKE` behaviour is
   unverified against it.
3. **Isolation level untested.** `TestCase` runs each test in a transaction, so
   true `READ COMMITTED` multi-connection behaviour (the only level where
   oversell/double-spend actually manifest) was **not** exercised. Closing this
   needs the RUN-1 load/stress harness already queued behind this task — on
   SQLite it would be theatre, exactly as the ledger notes.
4. **Connection pooling / `CONN_MAX_AGE`** untested (single-process run).
5. **Migrations were not exercised against a populated schema** — only
   `makemigrations --check` (clean) and a fresh `migrate` (all applied OK).
   Forward-migration behaviour on existing Postgres data is unmeasured.
6. **`psycopg2` is not installed.** Only psycopg3 was exercised. Anything
   naming `psycopg2` in a dependency path is untested here.

---

## 8. Recommended follow-ups (none applied by PG-1)

Ordered by severity. All are separate tasks.

1. **P0** `orders/views.py:1667` — `verify_payment` is dead on production
   Postgres. (A1)
2. **P1** `orders/admin.py:476/544/548/644` — admin bulk actions dead on
   production. (A2)
3. **P1** slug truncation must be unconditional, not collision-only; then flip
   `test_slug_overflow_without_collision_currently_exceeds_max_length`. (A3)
4. **P1** land the CI matrix from section 2 so the gate stops being SQLite-only.
5. **P2** fix the 4 test-only assumptions (B1/B2/B3) so the Postgres leg can go
   green and stay green.
6. **P2** audit every `expectedFailure` in the repo for exceptions-vs-assertion
   drift (section 4, A3). The xfail count cannot detect it.
7. **P2** the `load_dotenv()` production hazard (6.1).
8. **P2** `scripts/doc_claims.py` cannot distinguish a `test_*.py` module
   reference from a `test_*` function reference, so every citation of a
   `test_`-stemmed module in added prose is a hard error. This already affects
   committed content (`docs/run/section-12.md` line 18) and is latent in
   `changes.md`, which is full of `tests/test_*.py` citations. It was left
   alone here: changing a build gate inside the commit that measures the suite
   would make the measurement unauditable.