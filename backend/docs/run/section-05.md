# Section 5 — Admin panel — complete specification (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 1269–1424.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-5-01 | 5 | Dashboard KPI gaps: aggregate Orders card (orders_total computed but not rendered), Average order value (missing), true pending-fulfilment metric (currently shows payment-pending, ops/services.py:38) — get_stats + dashboard.html + ops tests | 1269–1424 [5.4],[5.5],[5.6] | PR-OPENED (pushed 7255b3c..10857a5; PR #2 comment 5727168338; cycle-1 SHIP) | 1 |
| SPEC-5-02 | 5 | Ops convention conformance: LOW_STOCK_THRESHOLD hardcoded (ops/services.py:5) → env-driven; N+1 per-order User.objects.get in dashboard view (ops/views.py:42-48) → batch/select_related | 1269–1424 + conventions.md:23 | PR-OPENED (pushed; PR #2 comment 5727417425; cycle-1 SHIP. NOTE: push race carried unaudited ed04dff to remote — remediation: refspec-push policy) | 1 |
| SPEC-5-03 | 5 | "Sales over time" chart from real order/payment data (time-series aggregation + dashboard chart block + tests) — fulfils SPEC-1-10 | 1269–1424 [5.7],[5.9] | PR-OPENED (ed04dff verified; PR #2 comment 5727562233; cycle-1 SHIP) | 1 |
| SPEC-5-04 | 5 | Catalogue admin surfaces: Category/Collection/Brand/Attribute/Review models+admins, media library — Owner: S8/S9 (cross SPEC-3-19/SPEC-3-20) | 1269–1424 [5.11] | PENDING | 0 |
| SPEC-5-05 | 5 | Fulfilment admin: Warehouses, Shipments, Shipping rules — Owner: S10/S12 (cross SPEC-1-07/SPEC-1-08) | 1269–1424 [5.12] | PENDING | 0 |
| SPEC-5-06 | 5 | Marketing admin: Campaigns, Gift cards (Newsletter subscribers → SPEC-3-10) — Owner: S6 (cross SPEC-1-17/SPEC-3-20) | 1269–1424 [5.13] | PENDING | 0 |
| SPEC-5-07 | 5 | Storefront content admin: Pages & content, Navigation menus, Homepage builder, SEO settings — Owner: S16 (cross SPEC-3-25/SPEC-3-09) | 1269–1424 [5.14] | PENDING | 0 |
| SPEC-5-08 | 5 | Operations admin: Support inbox/tickets, Activity logs, Import, Background jobs — Owner: S6/S7/S19 (cross SPEC-1-15/SPEC-1-11/SPEC-2-03) | 1269–1424 [5.15] | PENDING | 0 |
| SPEC-5-09 | 5 | Administration surfaces: Roles & permissions UI, Tax settings, Integrations, Payment settings surface — Owner: S6/S17/S16 | 1269–1424 [5.16] | PENDING | 0 |
| SPEC-5-10 | 5 | Unified global admin search (cross-entity "Search orders, products, customers…") — Owner: S20 | 1269–1424 [5.2] | PENDING | 0 |
| SPEC-5-11 | 5 | Payments admin surface + Returns & refunds nav (needs refund model) — Owner: S11 (cross SPEC-1-05/SPEC-1-18) | 1269–1424 [5.10] | PENDING | 0 |
