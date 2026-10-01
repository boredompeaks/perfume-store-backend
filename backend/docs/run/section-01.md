# Section 1 — System overview (task ledger)

Spec lines: **29–151**. Re-derived 2026-10-01 by compliance against `spec-comp` @ `f3ee7cc`
(reconciles the orphaned-deferral sweep: every owner section S3–S22 has closed its queue).

## Reconciliation of the 18 deferred rows

Owner attribution is hereby **re-assigned to S1** — the owner sections closed without building.

| Row | Verdict | Evidence / gap |
|---|---|---|
| SPEC-1-04 | OPEN | no compare endpoint/route/page; only hmac.compare_digest |
| SPEC-1-05 | OPEN | no Refund model; `partially_refunded` choice has NO writer (orders/state.py:134) |
| SPEC-1-06 | OPEN | webhooks deliberately unrouted (config/urls.py:37), absence test-pinned (config/tests.py:80) |
| SPEC-1-07 | OPEN | Order has 6 free-text address fields, no rate/cost entity (ops/services.py:21 comment only) |
| SPEC-1-08 | OPEN | no carrier/tracking_number on Order; no Shipment model |
| SPEC-1-09 | OPEN | no ReturnRequest model; returns page is a manual-email policy page |
| SPEC-1-10 | **BUILT** | `get_sales_series` ops/services.py:27-79 wired ops/views.py:75 + tests ops/tests.py:275-394 — ledger was STALE |
| SPEC-1-11 | **BUILT** | _build_logging settings.py:708 + AuditEvent common/models.py:36 + audit suite |
| SPEC-1-12 | OPEN | registry has ONE handler (ORDER_PAID); all other events no-op (common/notifications.py:97) |
| SPEC-1-13 | OPEN + DEVIATES | `Order.user` non-null FK; checkout `@IsAuthenticated` — contradicts spec line 74 |
| SPEC-1-14 | OPEN | no Address model, no Review model, no /account route |
| SPEC-1-15 | OPEN (partial) | support role+enforcement BUILT (common/roles.py:41-45); enquiry/ticket entity ABSENT |
| SPEC-1-16 | OPEN + DEVIATES | dispatch status EXISTS (orders/models.py:173); no Packlist/pick-list; `orders.fulfill` not on inventory role |
| SPEC-1-17 | OPEN | no Campaign/Banner model; storefront hero is static markup |
| SPEC-1-18 | OPEN | reconciliation manual by design; single flat export_csv; no settlement/payout ledger |
| SPEC-1-19 | **BUILT** | Group-per-role sync common/roles.py:53 + role-scoped admin accounts/admin.py:161 — ledger was STALE |
| SPEC-1-20 | OPEN | exactly 6 roles, no ROLE_SUPERADMIN; only implicit Django is_superuser bypass |
| SPEC-1-21 | PARTIAL + DEVIATES | capability classes enforced broadly; inventory/fulfilment cannot pack/ship (roles.py:39-42) |

SHIPPED rows re-verified built: SPEC-1-01 @ 5b88c43, SPEC-1-02/03 @ 002d8e3, SPEC-1-22 @ 7255b3c.

## Build queue (sizing gate applied — 4 oversized tasks split pre-dispatch)

| Task ID | Req | Pri | Scope (files) | Status |
|---|---|---|---|---|
| SPEC-1-B01 | Refund model + gateway refund call + admin refund endpoint (atomic + select_for_update) | P1 | 5 / 1 model | PENDING |
| SPEC-1-B02 | Razorpay webhook ingest, signature-verified, idempotent PaymentEvent, capture→confirm reconciler | P1 | 5 / 1 model | PENDING (dep B01) |
| SPEC-1-B03 | Superadmin tier + platform capabilities; grant inventory/fulfilment the packing capability | P2 | 5 / 0 | PENDING |
| SPEC-1-B04 | Guest checkout: nullable Order.user + guest_email/guest_token, order lookup, drop auth wall | P2 | 5 / 0 | PENDING (dep B01,B02) |
| SPEC-1-B05 | ShippingRate/ShippingMethod + checkout cost application | P2 | 5 / 1 model | PENDING |
| SPEC-1-B06 | Shipment + ShipmentEvent, admin columns, customer track-by-order_number | P2 | 5 / 1 model | PENDING (dep B05) |
| SPEC-1-B07a | ReturnRequest lifecycle core (model/admin/views/urls/routing) | P2 | 5 / 1 model | PENDING (dep B01) |
| SPEC-1-B07b | Returns serializers + API tests + customer request form | P2 | 3 | PENDING (dep B07a) |
| SPEC-1-B08a | Order shipped/delivered email handlers + templates | P2 | 5 | PENDING |
| SPEC-1-B08b | Order receipt email (split from B08a — 3rd template would spill) | P3 | 2 | PENDING (dep B08a) |
| SPEC-1-B09 | Address model + CRUD + checkout picker + /account page | P2 | 5 / 1 model | PENDING (dep B04) |
| SPEC-1-B10 | Settlement/payout ledger + reconciliation report | P2 | 5 / 1 model | PENDING (dep B01) |
| SPEC-1-B11 | Product compare endpoint + storefront compare tray | P3 | 5 / 0 | PENDING |
| SPEC-1-B12 | Dashboard reports depth: date-range, comparison period, metrics CSV (= SPEC-6-07, re-attributed S6→S1) | P3 | 4 / 0 | PENDING |
| SPEC-1-B13 | Packlist model + pick-list + dispatch confirmation | P3 | 5 / 1 model | PENDING (dep B03) |
| SPEC-1-B14a | Support Enquiry ticket entity + inbox (core) | P3 | 5 / 1 model | PENDING |
| SPEC-1-B14b | Enquiry serializers + tests (split from B14a) | P3 | 2 | PENDING (dep B14a) |
| SPEC-1-B15a | Campaign/Banner entity + scheduling (core) | P3 | 5 / 1 model | PENDING |
| SPEC-1-B15b | Campaign storefront slot + serializers/tests (split from B15a) | P3 | 3 | PENDING (dep B15a) |

Sequencing: B01 first (gates B02 trust, B07a/B10 refunds, B04). All migration-bearing tasks
dispatch SEQUENTIALLY — never in parallel (orders/accounts model contention).