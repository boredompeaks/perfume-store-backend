# Test gaps — FLAGGED

Runtime-confirmed: `manage.py test` → **Found 0 test(s)**.

## Current state

| Metric | Count |
|---|---|
| Endpoints (API routes) | 18 |
| Unit tests written | **0** |
| E2E / integration tests written | **0** |
| Code under automated test | **0%** |

An ad-hoc 20-step e2e smoke run (manual, not committed, in-memory DB) passed 20/20 — but it is throwaway and not repeatable in CI. Everything below must become permanent tests.

## Unit tests to write (39 minimum)

### accounts (14)
1. RegisterSerializer: valid data creates inactive user
2. RegisterSerializer: duplicate username (iexact) rejected
3. RegisterSerializer: duplicate email (iexact) rejected
4. RegisterSerializer: password < 8 rejected
5. RegisterSerializer: **missing `validate_password` policy** (documents V-05 — common/numeric passwords accepted; flip test when fixed)
6. register view: SMTP failure → 503, account still created (F-28)
7. verify-email: valid token activates user
8. verify-email: invalid/expired token → 400
9. resend-verification: unknown email → uniform 200 (no enumeration)
10. forgot-username: unknown email → uniform 200
11. password-reset request: inactive-only filter
12. password-reset confirm: weak password → 400 with validator messages
13. password-reset confirm: valid → password changed, old token invalidated
14. username-available: <3 chars, taken, free

### products (8)
15. list: search across name/description/category
16. list: category filter (iexact), min/max price incl. invalid decimal → 400
17. list: ordering whitelist (invalid value ignored, not 500)
18. list: pagination envelope shape; **assert ordering stability** (documents F-12)
19. slug auto-generation + collision suffix
20. create/update/delete require staff (403 for anon + normal user) — one test per method
21. detail 404 for unknown slug
22. serializer: explicit fields (will break when `__all__` replaced — intentional)

### cart (7)
23. GET creates session cart lazily
24. add item: new row created with correct quantity
25. add item: existing row quantity accumulates
26. add item: quantity > stock → 400 (both new and accumulate paths)
27. patch quantity: 0/negative/non-numeric → 400; > stock → 400
28. delete item: removed; response excludes session_id (will flip when F-15 fixed)
29. cross-session isolation: item from another session → 404

### orders (10)
30. coupon: inactive/expired/not-yet-valid/usage-limit/min-order rejections (parametrized)
31. coupon: percentage cap via `maximum_discount`
32. coupon: discount clamped at subtotal (fixed > subtotal)
33. coupon: **rounding parity** preview vs checkout (documents F-11 — fails today)
34. checkout: server-side total from DB prices (client-sent amounts ignored)
35. checkout: missing required field → 400 listing field
36. create_payment: reuses existing `razorpay_order_id` (mock Razorpay); order scoped to owner (404 for other user); non-pending → 400
37. verify_payment: forged signature → 400 (mock utility to raise)
38. verify_payment: success path — stock decremented, coupon.used_count++, cart cleaned, status confirmed (mock signature pass)
39. verify_payment: idempotency — second verify → 400; insufficient stock at verify → 409 **and refund TODO** (documents V-03)

## E2E / integration tests to write (12 minimum)

1. Full happy path: register → verify → login → product (staff) → cart add → checkout → payment (mocked Razorpay) → verify → stock/coupon/cart assertions
2. Register → never verify → login blocked forever (until resend)
3. Password reset end-to-end via outbox links (like audit smoke #16)
4. Multi-user isolation: user B cannot see/checkout user A's order or cart
5. Oversell race: two concurrent checkouts, one item left → exactly one verify succeeds
6. Coupon race: two concurrent verifications on usage_limit=1 → exactly one increments
7. Product lifecycle: create → patch price → order on old price (snapshot check) → delete product → order items retain `product_name`
8. Cart persistence across requests with same session cookie; cleared after paid verify
9. Pagination stability across pages with equal created_at values (documents F-12)
10. Unverified user cannot checkout (JWT is issued? no — login blocked; test the 401 → checkout 401 chain)
11. Anonymous cart + JWT checkout without session cookie → documented 404 (flip to 400 + guidance when F-18 fixed)
12. Admin status transition: confirmed → shipped → delivered; cancelled after payment must be blocked/reconciled (placeholder until refund flow exists)

## Infrastructure

- Mock/fake the Razorpay client (`unittest.mock.patch('orders.views.razorpay.Client')`) — no test may hit the live API (live test keys must be rotated and never used in tests).
- Add `coverage.py` gate: fail CI under 80%.
- CI (GitHub Actions): install pinned reqs → `manage.py check` → `makemigrations --check` → `manage.py test` → coverage report.
