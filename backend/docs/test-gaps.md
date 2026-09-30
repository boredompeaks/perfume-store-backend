# Test gaps — FLAGGED → DELIVERED (measured 2026-09-30)

The enumerated backlog below was written on 2026-09-18 as an open TODO list and
was implemented in full the same day (see the Phase 5 entry in
`docs/changes.md`). This revision re-measures the numbers from a real run and
re-frames each backlog item as what it BECAME, so the file records shipped
work instead of advertising an open list.

**Measured this run** (`manage.py test`, `--verbosity 0`):

```text
Ran 1021 tests
OK (expected failures=4)
```

```bash
venv\Scripts\python -m coverage run manage.py test
venv\Scripts\python -m coverage report
```

| Metric | Count |
|---|---|
| Tests | **1021 — 1017 pass + 4 expectedFailure (documented flip tests), 0 failures, 0 errors** |
| Code under automated test | **100.00%** (3909 statements, 0 missed; gate `fail_under = 90`) |
| API routes | **70** mounted under `/api/`: **34** in the legacy `/api/` family + **36** under `/api/v1/`. The v1 family re-mounts the same view objects rather than mirroring the legacy paths 1:1 — `products`/`cart`/`orders` move under `/api/v1/store/`, `accounts` under `/api/v1/account/`, the orders JSON seam under `/api/v1/admin/`, legacy `/api/settings/` reappears as `/api/v1/store/config/`, and v1 adds two admin seams (`dashboard`, `audit-log`) that the legacy family mounts outside `/api/` |
| Unit tests (app `tests.py` + `test_*.py`) | **500** (accounts 68, products 124, cart 50, orders 213, ops 45) |
| E2E / integration tests (`tests/` package) | **516** (cross-app, admin-surface, CSRF/audit/ops, checkout-refresh and restore-drill specs) |
| Settings / security specs (`config/tests.py`) | **5** (V-02 fail-closed DEBUG + secret-key subprocess checks) |

Unit tests live in each app's `tests.py`/`test_*.py`; e2e/integration tests in
the top-level `tests/` package. Razorpay is always mocked; the shared base class
(`common/testing.py`) forces locmem email, dummy Razorpay keys and a temp media
root, so no test can reach the network or the real keys from `.env`.

## What the backlog became

### accounts (14 enumerated) — DELIVERED in `accounts/tests.py` (68 tests)

1. RegisterSerializer: valid data creates inactive user — DELIVERED.
2. Duplicate username (iexact) rejected — DELIVERED.
3. Duplicate email (iexact) rejected — DELIVERED.
4. Password < 8 rejected — DELIVERED.
5. ~~missing `validate_password` policy (V-05)~~ — **CLOSED**: `validate_password`
   now runs on register, reset-confirm and every other credential path
   (`conventions.md`); the flip test flipped green and is now a plain pin
   (`test_v05_registration_rejects_common_password`,
   `test_v05_registration_rejects_numeric_only_password`).
6. register: SMTP failure → 503, account still created (F-28) — DELIVERED.
7. verify-email: valid token activates — DELIVERED.
8. verify-email: invalid/expired token → 400 — DELIVERED.
9. resend-verification: unknown email → uniform 200 (no enumeration) — DELIVERED.
10. forgot-username: unknown email → uniform 200 — DELIVERED.
11. password-reset request: inactive-only filter — DELIVERED.
12. password-reset confirm: weak password → 400 with validator messages — DELIVERED.
13. password-reset confirm: valid → password changed, old token invalidated — DELIVERED.
14. username-available: <3 chars, taken, free — DELIVERED.

One expectedFailure remains in this app, and it is a *different* item than #5:
registration still accepts a blank email (`User.email` is `blank=True`), pinned
by `test_registration_gap_missing_email_currently_accepted`.

### products (8 enumerated) — DELIVERED in `products/tests.py` + `test_variant.py` (124 tests)

15. Search across name/description/category — DELIVERED.
16. Category filter (iexact) + min/max price incl. invalid decimal → 400 — DELIVERED.
17. Ordering whitelist (invalid value ignored, not 500) — DELIVERED.
18. Pagination envelope shape — DELIVERED. The F-12 "default ordering is
    non-deterministic" sub-note is **CLOSED**:
    `test_f12_default_listing_order_is_deterministic` now pins a stable order.
19. Slug auto-generation + collision suffix — DELIVERED (via IntegrityError
    retry, not check-then-act).
20. create/update/delete require staff (403 for anon + normal user) — DELIVERED.
21. Detail 404 for unknown slug — DELIVERED.
22. Serializer explicit fields — DELIVERED (every serializer names its `fields`;
    `'__all__'` appears nowhere in the tree).

Variant selection (SPEC-6-08) was added later on top of this list; its
expectedFailure is the untruncated-slug pin
(`test_slug_overflow_without_collision_currently_exceeds_max_length`).

### cart (7 enumerated) — DELIVERED in `cart/tests.py` (50 tests)

23. GET creates the session cart lazily — DELIVERED.
24. Add item: new row with correct quantity — DELIVERED.
25. Add item: existing row quantity accumulates — DELIVERED.
26. Quantity > stock → 400 (new *and* accumulate paths) — DELIVERED.
27. PATCH quantity: 0 / negative / non-numeric → 400; > stock → 400 — DELIVERED.
28. Delete item: removed; response excludes `session_id` — DELIVERED for the
    removal. The `session_id` half is **still open (V-09/F-15)** and is the
    cart app's one expectedFailure
    (`test_v09_cart_payload_excludes_session_id`).
29. Cross-session isolation: another session's item → 404 — DELIVERED.

Coupon-as-cart-state (R-9.3.5/R-9.3.6, `POST`/`DELETE /api/cart/coupon/`) was
added after this list was written; its pins are in `CartCouponStateTests`.

### orders (10 enumerated) — DELIVERED in `orders/tests.py` + `test_idempotency.py` + `test_order_number.py` (213 tests)

30. Coupon rejections (inactive / expired / not-yet-valid / usage limit /
    min-order) — DELIVERED.
31. Percentage cap via `maximum_discount` — DELIVERED.
32. Discount clamped at subtotal — DELIVERED.
33. Rounding parity preview vs checkout (F-11) — **CLOSED**: amounts are
    `quantize`d before every compare/serialize, so
    `test_f11_rounding_parity_between_preview_and_checkout` is a plain pin.
34. Checkout totals from DB prices (client-sent amounts ignored) — DELIVERED.
35. Missing required field → 400 naming the field — DELIVERED.
36. create_payment: reuses existing `razorpay_order_id` (mocked), owner-scoped
    (404 otherwise), non-pending → 400 — DELIVERED.
37. verify_payment: forged signature → 400 — DELIVERED.
38. verify_payment success: stock decremented, `coupon.used_count++`, cart
    cleaned, status confirmed — DELIVERED.
39. verify_payment idempotency (second verify → 400) and insufficient stock at
    verify → 409 — DELIVERED. The "refund TODO (V-03)" half is **deliberately
    still manual**: there is no refund flow, so a paid-order cancel is refused
    by the state machine (`orders/state.py:30`) with the reconcile-manually
    wording, and the manual contract is documented in
    `docs/runbook-incident-recovery.md`.

Added after this list: order-number race retries, checkout dedup (SPEC-21-1),
Idempotency-Key replay (SPEC-9-01) and checkout-refresh safety (SPEC-21-5).

### E2E / integration (12 enumerated) — DELIVERED in `tests/` (516 tests)

1. Full happy path (register → verify → login → product → cart → checkout →
   mocked payment → verify → stock/coupon/cart) — DELIVERED
   (`FullPurchaseHappyPathTests`).
2. Register → never verify → login blocked — DELIVERED.
3. Password reset end-to-end via outbox links — DELIVERED.
4. Multi-user isolation (B cannot see/checkout A's order or cart) — DELIVERED.
5. Oversell race: two concurrent checkouts, one unit left → exactly one verify
   succeeds — DELIVERED (resolved through the verify-time row lock).
6. Coupon race: two concurrent verifications on `usage_limit=1` → exactly one
   increments — DELIVERED.
7. Product lifecycle: price change → order snapshots the old price; deleted
   product → items retain `product_name` — DELIVERED.
8. Cart persistence across requests; cleared after paid verify — DELIVERED.
9. Pagination stability across pages with equal `created_at` (F-12) — DELIVERED
   and, as above, the ordering gap it documented is closed.
10. Unverified user cannot checkout (401 chain) — DELIVERED.
11. Anonymous cart + JWT checkout without a session cookie — DELIVERED as the
    documented 404. The stronger F-18 contract (guidance on the 404) is the
    `tests/` package's one expectedFailure
    (`test_f18_jwt_checkout_without_session_should_guide_the_client`).
12. Admin status transition confirmed → shipped → delivered, and cancelling a
    paid order blocked — DELIVERED; the paid-order case is a 409 /
    admin error message naming manual reconciliation, not a silent edit.

## Infrastructure

- Mock/fake the Razorpay client (`unittest.mock.patch('orders.views.razorpay.Client')`)
  — no test may hit the live API; the fixture keys in `common/testing.py` are
  recognizably non-credentials so an un-mocked client cannot use `.env` (V-01).
- `coverage.py` gate: `.coveragerc` `fail_under = 90` (measured 100.00%).
- CI (GitHub Actions, `.github/workflows/backend-tests.yml`): pinned reqs →
  `manage.py check` → `makemigrations --check` → `manage.py test` → coverage
  report.
