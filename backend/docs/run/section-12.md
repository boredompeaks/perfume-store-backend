# Section 12 — Inventory reservation and concurrency (task ledger)

Created 2026-09-19 from the S12 compliance prefetch. Spec lines: 3579–3633. Prefetch result: 12 rows — 4 IMPLEMENTED / 2 PARTIAL / 6 MISSING / 0 DEVIATES / 0 N-A.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-12-01 | 12 | Reservation data model: StockReservation expressing §12.1's on-hand/reserved/available-to-sell split, reserved quantity per product+order reference, expiry timestamp, owner FK, status lifecycle, env-driven RESERVATION_TTL setting (pattern: ops/services.py:9); migration + model tests. Reserved/safety-field accounting depth stays with SPEC-6-13 | 3579–3633 §12.1 [R-12.2],[R-12.3],[R-12.4],[R-12.10] | SHIPPED (FULL audit cycle-1 @ c69f8cf: 626=622+4xf cov 100.00% @2490, 0009 dance OK, scope exact, views.py untouched) | 1 |
| SPEC-12-02 | 12 | Checkout reservation lifecycle: create time-limited reservations inside create_order's atomic block, convert reservation→committed sale inside verify_payment's existing locked block (replacing the blind decrement, keeping the 409 re-check as backstop), release on failed/cancelled/expired checkout; deterministic product-lock ordering (order_by('id')) to close the deadlock advisory | 3579–3633 §12.2 [R-12.6],[R-12.7],[R-12.8] | PENDING (from S12 compliance prefetch; depends on SPEC-12-01) | 0 |
| SPEC-12-03 | 12 | Scheduled stale-reservation reconciliation: management command expire_reservations releasing expired reservations via the StockMovement ledger, env-driven cadence documented and coordinated with payment-provider timeout (cross SPEC-1-06/SPEC-1-18); covers docs/audit.md:35 F-26 pending-order staleness for reserved stock | 3579–3633 [R-12.9] | PENDING (from S12 compliance prefetch; depends on SPEC-12-01) | 0 |

Owner attributions (no duplicate tasks):
- R-12.1 (no hard oversell), R-12.5 (stock validation at cart/checkout) → **SPEC-6-01** (SHIPPED — gate + verify backstop delivered); the residual pay-then-409 money exposure is owned by **SPEC-1-05/SPEC-1-06** (refund/webhook side).
- R-12.11 (atomic+locked single operation), R-12.12 (no check-then-act on stock control) → **SPEC-6-02** (SHIPPED — adjust_stock) + in-place verify path (orders/views.py:573-651).
- Reserved/safety field accounting depth → **SPEC-6-13**; REST adjustment endpoint → **SPEC-9-06**.

Verified facts (2026-09-19 prefetch): hard oversell IMPOSSIBLE — both stock-reducing paths (verify_payment, adjust_stock) re-check sufficiency under the lock they subtract under; regression-pinned at tests/test_e2e_concurrency.py:20-76. Soft overbooking (two pending orders on the last unit, loser 409'd after capture) is by-design; §12's reservation model exists to prevent it. Advisory (not a violation): verify_payment acquires product locks in cart-insertion order (orders/views.py:604) — deadlock-possible across carts sharing products in different orders; loser gets a clean 500 rollback; deterministic ordering pinned into SPEC-12-02.
