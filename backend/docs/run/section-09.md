# Section 9 — API design — the complete backend contract (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 2571–3404. Compliance prefetch 2026-09-18: 71 rows — 15 IMPLEMENTED / 21 PARTIAL / 32 MISSING / 3 DEVIATES / 0 N-A. Build-now queue SPEC-9-01..08. DEVIATES: R-9.0 unversioned routes, R-9.3.7/12 no checkout-session entity.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-9-01 | 9 | P1 — Checkout submit idempotency + duplicate-order guard: Idempotency-Key on POST checkout, unique-by-key dedupe inside existing atomic block, regression tests | 2571–3404 [R-9.3.14],[R-9.3.19] | PENDING (from S9 compliance prefetch; §12 keeps reservation concurrency) | 0 |
| SPEC-9-02 | 9 | P2 — /api/v1/ namespace + prefix mapping (store/account/admin/webhooks), legacy-path aliases, dual-mount tests; coordinate frontend base URL with S3 before cutover | 2571–3404 [R-9.0 DEVIATES] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-03 | 9 | P2 — Uniform error envelope: one helper converting DRF field errors + {"error":…} into a single response shape, envelope-pinning tests | 2571–3404 [R-9.2.19] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-04 | 9 | P2 — Customer order-detail endpoint GET /api/orders/<id>/ owner-checked + pagination on order history | 2571–3404 [R-9.2.14],[R-9.2.15] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-05 | 9 | P2 — Coupon apply/remove as cart state: POST/DELETE /api/cart/coupon/ persisting coupon FK on Cart, re-validated at checkout; depth policy stays SPEC-6-09 | 2571–3404 [R-9.3.5],[R-9.3.6] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-06 | 9 | P3 — REST inventory-adjustment endpoint reusing adjust_stock + HasInventoryAdjust (already defined, unused), ledger-assertion tests | 2571–3404 [R-9.4.7] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-07 | 9 | P3 — Admin orders JSON seam: GET /api/admin/orders/ + /:id/ (orders.read), POST fulfill/cancel reusing transition_allowed + HasOrdersFulfill/HasOrdersCancel; INCLUDES P3 hardening: re-apply status__in predicate in _bulk_set_status WHERE (SPEC-6-04 audit advisory) | 2571–3404 [R-9.4.8]–[R-9.4.11] + SPEC-6-04 advisory | PENDING (from S9 compliance prefetch; feature depth stays SPEC-6-11) | 0 |
| SPEC-9-08 | 9 | P3 — Write-endpoint contract doc: per-endpoint 10-point table (R-9.5 checklist) as backend/docs/api-contract.md (docs-only) | 2571–3404 [R-9.5] | PENDING (from S9 compliance prefetch) | 0 |

Most MISSING rows attribute to existing owner tasks: wishlist SPEC-3-17, addresses SPEC-3-16, reviews SPEC-3-19, guest checkout SPEC-3-02/1-13, refunds SPEC-1-05/6-12, shipping SPEC-3-12/1-07, content SPEC-3-25/5-07, staff surfaces SPEC-6-05, audit SPEC-7-01, publish SPEC-6-08.
