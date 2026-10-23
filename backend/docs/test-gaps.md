# Test gaps — FLAGGED → DELIVERED (enumerated 2026-09-18; delivery measured 2026-09-30; floor cited 2026-10-23)

The enumerated backlog below was written on 2026-09-18 as an open TODO list and
was implemented in full the same day (see the Phase 5 entry in
`docs/changes.md`). The sections re-frame each backlog item as what it BECAME,
so the file records shipped work instead of advertising an open list: every
enumerated gap is marked DELIVERED or CLOSED against the shipped tree, and the
coverage floor carries **0 missed statements**.

**Current floor** — cited from the committed artifact `scripts/figures/P0-2.json`
(printed by `scripts/changelog_figures.py floor --task P0-2`, measured
2026-10-23 on both engines of the two-engine floor, the PostgreSQL leg against
lane B's `pg2b` container). The promoted PR #48 CI run (backend-tests
`39961186676`, recorded in `backend/docs/run/BUILD-QUEUE.md`) reported the same
2468 tests and 2 expected failures at 99.83% on its Linux leg; the earlier
promotion's row attributes that 0.01 point difference to host-conditional
branches rather than code drift:

```text
sqlite:     Ran 2468 tests, OK (expected failures=2), cov 99.84% (14005 stmts, 0 missed, 1750 branches, 25 partial)
postgresql: Ran 2468 tests, OK (expected failures=2), cov 99.84% (14005 stmts, 0 missed, 1750 branches, 25 partial)
```

The floor above was measured by `scripts/changelog_figures.py floor --task P0-2`
onto the committed artifact, not by the wrapper; `scripts/test-evidence.ps1` is
the per-run evidence wrapper, and it keeps the suite's own exit code, refuses
stale coverage data, writes exactly one artifact per engine and fails a total
under `.coveragerc`'s `fail_under`. Re-run at this tip on both engines, it
writes the same canonical totals to its per-engine artifacts --
`coverage-sqlite.json` and `coverage-pg.json` each report 14005 statements and
0 missed at 99.84%, matching `coverage report`:

```powershell
# SQLite
scripts\test-evidence.ps1 -Engine sqlite -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"
# PostgreSQL: export DATABASE_URL and REDIS_URL for the leg first, then
scripts\test-evidence.ps1 -Engine pg -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"
```

| Metric | Count |
|---|---|
| Tests | **2468 on each engine, OK, 2 expectedFailure** — the two remaining flips are named where they live (the accounts missing-email pin and the F-18 checkout-guidance pin below); figure from the artifact cited above |
| Code under automated test | **99.84%** (14005 statements, **0 missed**; gate `fail_under = 90`) — same artifact |
| API routes | mounted in two families from one urlconf — the legacy `/api/...` paths and the `/api/v1/` namespace (`store`, `account`, `admin` sub-families re-mounting the same view objects); exact per-family counts were a 2026-09-30 measurement and were not re-derived in this docs-only pass |
| Unit tests (app `tests.py` + `test_*.py`) | per-app counts in the section headings are the 2026-09-30 delivery measurement (historical) |
| E2E / integration tests (`tests/` package) | as above — historical delivery measurement |
| Settings / security specs (`config/tests.py`) | as above — historical delivery measurement |

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

Variant selection (SPEC-6-08) was added later on top of this list and carries
no expectedFailure of its own (products holds none — the untruncated-slug flip
it once shipped was un-xfailed by PG-2b and survives as the plain pin
`test_long_name_without_collision_truncated_to_slug_max_length`; the tree's
two remaining flips are the accounts missing-email pin and the e2e F-18 pin
named in the E2E section below).

### cart (7 enumerated) — DELIVERED in `cart/tests.py` (50 tests)

23. GET creates the session cart lazily — DELIVERED.
24. Add item: new row with correct quantity — DELIVERED.
25. Add item: existing row quantity accumulates — DELIVERED.
26. Quantity > stock → 400 (new *and* accumulate paths) — DELIVERED.
27. PATCH quantity: 0 / negative / non-numeric → 400; > stock → 400 — DELIVERED.
28. Delete item: removed; response excludes `session_id` — DELIVERED for the
    removal, and the `session_id` half is closed too (V-09/F-15, Q-S1):
    `CartSerializer` no longer carries the field, and
    `test_v09_cart_payload_excludes_session_id` pins the real unauthenticated
    `GET /api/cart/` body — no `session_id` key, and no raw session key
    anywhere in the payload.
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
- `coverage.py` gate: `.coveragerc` `fail_under = 90` (measured 99.84% with 0
  missed lines on both engines, per the floor artifact cited at the top). The
  capture runs through `scripts/test-evidence.ps1`, which preserves the suite's
  exit code and writes one artifact per engine — the two things the earlier
  piped evidence files got wrong.
- CI (GitHub Actions, `.github/workflows/backend-tests.yml`): pinned reqs →
  `manage.py check` → `makemigrations --check` → `manage.py test` → coverage
  report.
