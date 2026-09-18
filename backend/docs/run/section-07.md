# Section 7 — Backend architecture (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 2263–2453. Compliance prefetch 2026-09-18: 4 IMPLEMENTED / 12 PARTIAL / 4 MISSING / 0 DEVIATES.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-7-01 | 7 | Audit trail: append-only audit-log model + write hooks for order/payment/auth events (reuse StockMovement pattern) + migration + tests — fulfils the audit-trail half of SPEC-1-11 | 2263–2453 [R-7.20] + S1[1.22] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-02 | 7 | Logging baseline: env-driven LOGGING dict in settings (request errors + payment failures), wired so audit events and verify_payment failures emit — fulfils logging halves of SPEC-1-11/SPEC-2-08 | 2263–2453 + S1[1.22], S2[2.12] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-03 | 7 | Coupon quantize parity: quantize discount Decimals before compare/serialize in preview (orders/views.py:239-241,414-418) + checkout paths — flips pinned expectedFailure F-11 (orders/tests.py:190) | 2263–2453 [R-7.7 conventions finding 1] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-04 | 7 | Search hardening: env-driven pagination page size (products/views.py:94 hardcoded 2, F-23) + stable default ordering (F-12, flips pinned expectedFailure) in products listing | 2263–2453 [R-7.6 conventions findings 5] | PENDING (from S7 compliance prefetch) | 0 |

Cross-referenced, no new deferred rows: R-7.3→SPEC-1-14 · R-7.5/7.8→SPEC-6-13 · R-7.10→SPEC-3-12 · R-7.12→SPEC-1-05/06/18 · R-7.13→SPEC-6-09 · R-7.14→SPEC-1-07/08 · R-7.15→SPEC-6-12/3-18 · R-7.16→SPEC-1-14 · R-7.17→SPEC-2-03/S19 · R-7.18→SPEC-3-25/S16. Convention findings on touch: slug/uniqueness IntegrityError retry, plural model name, function views — grandfathered, align when touched.
