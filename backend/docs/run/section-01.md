# Section 1 — System overview (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 29–151.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-1-01 | 1 | Staff-gated product writes must use DRF `permission_classes` (never inline `request.user.is_staff`, products/views.py:113,172,198,225); products serializer must declare explicit `fields` (no `'__all__'`, products/serializers.py:8) | 29–151 + conventions.md:14,18 | PR-OPENED (auditor SHIP cycle-1; commit 5b88c43; 186 pass, cov 100.00%; PR #2) | 1 |
| SPEC-1-02 | 1 | Public mutating endpoints need throttle scopes (apply_coupon, cart mutations, login/register) and anonymous coupon errors must be uniform — no coupon existence/validation-state leaks (orders/views.py:284-285) | 29–151 + conventions.md:24,25 | PR-OPENED (pushed 5b88c43..002d8e3; PR #2 comment 5726186620; cycle-2 SHIP) | 1 |
| SPEC-1-03 | 1 | Registration must run `validate_password` (same policy as reset; accounts/serializers.py:7-10 only enforces min_length=8) | 29–151 + conventions.md:19 | PR-OPENED (pushed 5b88c43..002d8e3; PR #2 comment 5726186620; cycle-2 SHIP) | 1 |
| SPEC-1-04 | 1 | Product-compare feature absent (grep zero hits) — Owner: S3 | 29–151 [1.3] | PENDING | 0 |
| SPEC-1-05 | 1 | Refunds absent — no Razorpay refund call, no refund model/status; paid orders irreversible — Owner: S11 | 29–151 [1.14] | PENDING | 0 |
| SPEC-1-06 | 1 | Payment webhooks absent — payment truth only via client callback; dropped callback strands captured money — Owner: S11 | 29–151 [1.15] | PENDING | 0 |
| SPEC-1-07 | 1 | Shipping is status-flag only — no rates/costs/carrier/dispatch — Owner: S9 (cross S16) | 29–151 [1.17] | PENDING | 0 |
| SPEC-1-08 | 1 | Carrier tracking absent — no tracking number/carrier/shipment events on Order — Owner: S10 | 29–151 [1.18] | PENDING | 0 |
| SPEC-1-09 | 1 | Customer return requests absent — no RMA model/lifecycle; manual email + ledger only — Owner: S9 (cross S3) | 29–151 [1.19] | PENDING | 0 |
| SPEC-1-10 | 1 | Analytics are point-in-time only — no time-series/trends/reports — Owner: S5 | 29–151 [1.21] | PENDING | 0 |
| SPEC-1-11 | 1 | No LOGGING config; no audit trail for orders/payments/auth beyond StockMovement — Owner: S7 | 29–151 [1.22] | PENDING | 0 |
| SPEC-1-12 | 1 | Zero order-lifecycle notifications (confirmation/receipt/shipped/delivered) — Owner: S19 | 29–151 [1.23] | PENDING | 0 |
| SPEC-1-13 | 1 | Guest checkout DEVIATES: spec promises optional guest checkout; code hard-requires verified account (orders/views.py:45-47; CheckoutView.tsx:51-62) — Owner: S3 | 29–151 [1.25] | PENDING | 0 |
| SPEC-1-14 | 1 | Customer capabilities missing: saved addresses, product reviews, profile editing (3 of 5 claimed) — Owner: S9 (cross S3) | 29–151 [1.26] | PENDING | 0 |
| SPEC-1-15 | 1 | Support-agent role absent — no groups/roles/enquiry tooling — Owner: S6 | 29–151 [1.27] | PENDING | 0 |
| SPEC-1-16 | 1 | No packing/dispatch workflow for fulfilment operators — Owner: S6 | 29–151 [1.29] | PENDING | 0 |
| SPEC-1-17 | 1 | No campaign entity (banners/promos/scheduling) for marketing manager — Owner: S6 (cross S16) | 29–151 [1.30] | PENDING | 0 |
| SPEC-1-18 | 1 | Finance reconciliation incomplete — no refunds to reconcile; reports = flat CSV — Owner: S11 (cross S6) | 29–151 [1.31] | PENDING | 0 |
| SPEC-1-19 | 1 | No roles management (Group/permission model+UI); admin cannot manage roles/operational access — Owner: S6 | 29–151 [1.32] | PENDING | 0 |
| SPEC-1-20 | 1 | No superadmin tier — nothing distinguishes Admin vs Superadmin surfaces — Owner: S17 | 29–151 [1.33] | PENDING | 0 |
| SPEC-1-21 | 1 | Role-separation invariant (line 150) unenforced — any staff user can edit coupons/orders/users/settings — Owner: S17 | 29–151 [1.34] | PENDING | 0 |
| SPEC-1-22 | 1 | Throttle remaining public mutating endpoints: verify-email, resend-verification, forgot-username, password-reset(+confirm), token/refresh — prioritize email-sending endpoints (spam/bomb vectors) | conventions.md:24 + SPEC-1-02 auditor finding | PR-OPENED (pushed 002d8e3..7255b3c; PR #2 comment 5727047205; cycle-1 SHIP) | 1 |
