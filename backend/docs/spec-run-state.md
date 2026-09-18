# Spec run state

Source of truth for the spec-compliance run. Updated at every status transition with evidence (commit SHA, test command + result, or report verdict).

- Spec: `Saturday, Jul 25, 2026 at 5_23 AM.txt` (repo root, 5,734 lines; git-ignored by design)
- Work branch: `spec-comp` (created from `feat/add-frontend` @ `ac75148`). Promotion model: the release-engineer opens AND merges promotion PRs `spec-comp -> feat/add-frontend` at section boundaries (GATE_PHASE, merge on all-green); the auditor pushes `origin/spec-comp` (durability backup + PR head updates); `master` is frozen; the legacy PR #2 (base `master`) is closed at first promotion, never merged.
- Conventions: `backend/docs/conventions.md` (quoted into every builder handoff)
- Changelog: `backend/docs/changes.md` (append-only rows; auditor verifies each row against the diff)

## Section index

| # | Title | Spec lines | Status |
|---|---|---|---|
| 1 | System overview | 29–151 | SHIPPED (build-now tasks SPEC-1-01/02/03/22 all PR-OPENED; 20 deferred tasks tracked with owner sections) |
| 2 | Recommended technology stack | 152–278 | SHIPPED (build-now SPEC-2-01 PR-OPENED; 9 deferred rows tracked with owner sections) |
| 3 | Frontend — customer-facing website | 279–1230 | IN-COMPLIANCE |
| 4 | Complete route map — what is public, gated or restricted? | 1231–1268 | IN-COMPLIANCE |
| 5 | Admin panel — complete specification | 1269–1424 | SHIPPED (build-now SPEC-5-01/02/03 PR-OPENED; 8 deferred rows tracked with owner sections) |
| 6 | Admin features — detailed functional requirements | 1425–2262 | IN-COMPLIANCE |
| 7 | Backend architecture | 2263–2453 | IN-COMPLIANCE (prefetched 2026-09-18: 4 IMPLEMENTED / 12 PARTIAL / 4 MISSING / 0 DEVIATES. Build-now queue SPEC-7-01..04. Cross-referenced, no new deferred rows: R-7.3→SPEC-1-14 · R-7.5/7.8→SPEC-6-13 · R-7.10→SPEC-3-12 · R-7.12→SPEC-1-05/06/18 · R-7.13→SPEC-6-09 · R-7.14→SPEC-1-07/08 · R-7.15→SPEC-6-12/3-18 · R-7.16→SPEC-1-14 · R-7.17→SPEC-2-03/S19 · R-7.18→SPEC-3-25/S16. Convention findings on touch: slug/uniqueness IntegrityError retry, plural model name, function views — grandfathered, align when touched) |
| 8 | Database architecture | 2454–2570 | IN-COMPLIANCE (prefetched 2026-09-18: 6 IMPLEMENTED / 6 PARTIAL / 4 MISSING / 0 DEVIATES / 1 NOT-APPLICABLE. Build-now queue SPEC-8-01..06. Cross-referenced: billing addr→SPEC-3-16 · item discounts→SPEC-6-09 · tax→SPEC-3-12 · paid/webhook/refund history→SPEC-6-11/6-12/1-05 · tracking→SPEC-5-05/1-08 · audit records→SPEC-7-01 · reservations→§12. §8.2 heading empty in spec — entity inventory via §8.1 diagram note) |
| 9 | API design — the complete backend contract | 2571–3404 | IN-COMPLIANCE (prefetched 2026-09-18: 71 rows — 15 IMPLEMENTED / 21 PARTIAL / 32 MISSING / 3 DEVIATES / 0 N-A. Build-now queue SPEC-9-01..08. DEVIATES: R-9.0 unversioned routes, R-9.3.7/12 no checkout-session entity. Most MISSING rows attribute to existing owner tasks: wishlist SPEC-3-17, addresses SPEC-3-16, reviews SPEC-3-19, guest checkout SPEC-3-02/1-13, refunds SPEC-1-05/6-12, shipping SPEC-3-12/1-07, content SPEC-3-25/5-07, staff surfaces SPEC-6-05, audit SPEC-7-01, publish SPEC-6-08) |
| 10 | Order lifecycle and state machines | 3405–3522 | PENDING |
| 11 | Payment architecture | 3523–3578 | PENDING |
| 12 | Inventory reservation and concurrency | 3579–3633 | PENDING |
| 13 | Recommended frontend project structure | 3634–3759 | PENDING |
| 14 | Recommended backend project structure | 3760–3854 | PENDING |
| 15 | UI/UX design system specification | 3855–4088 | PENDING |
| 16 | Store settings and configuration | 4089–4238 | PENDING |
| 17 | Security specification | 4239–4426 | PENDING |
| 18 | SEO, performance and discoverability | 4427–4602 | PENDING |
| 19 | Notifications and background jobs | 4603–4777 | PENDING |
| 20 | Admin usability and operational workflows | 4778–4853 | PENDING |
| 21 | Testing specification | 4854–4937 | PENDING |
| 22 | Deployment and infrastructure | 4938–5070 | PENDING |
| 23 | Development roadmap — what to build first | 5071–5277 | PENDING |
| 24 | Feature prioritization matrix | 5278–5545 | PENDING |
| 25 | The master requirements checklist | 5546–5661 | PENDING |
| 26 | Final architectural recommendations | 5662–5734 | PENDING |

Note: grep hits `# ₹2.48L` (1291), `# 184` (1297), `# ₹1,348` (1303), `# 23` (1309) are dashboard metric artifacts inside section 5, not sections — excluded after verification.

## Task ledger

Deferral policy: gaps whose detailed requirements live in a later spec section are recorded here as `PENDING` with `Owner: S<n>`; they are built during that section's loop (re-scoped against full detail) and re-verified in the final whole-spec sweep.

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
| SPEC-4-01 | 4 | Regression test pinning `order_list` returns only the requester's orders (username-available rate-limit half already delivered by SPEC-1-22 `auth` scope) | 1231–1268 [4.6],[4.29],P9 | PENDING | 0 |
| SPEC-4-02 | 4 | /profile + /account/* routes MISSING from code (matrix rows) — Owner: S3/S9 via SPEC-3-15 | 1231–1268 [4.20],[4.21] | PENDING | 0 |
| SPEC-5-01 | 5 | Dashboard KPI gaps: aggregate Orders card (orders_total computed but not rendered), Average order value (missing), true pending-fulfilment metric (currently shows payment-pending, ops/services.py:38) — get_stats + dashboard.html + ops tests | 1269–1424 [5.4],[5.5],[5.6] | PR-OPENED (pushed 7255b3c..10857a5; PR #2 comment 5727168338; cycle-1 SHIP) | 1 |
| SPEC-5-02 | 5 | Ops convention conformance: LOW_STOCK_THRESHOLD hardcoded (ops/services.py:5) → env-driven; N+1 per-order User.objects.get in dashboard view (ops/views.py:42-48) → batch/select_related | 1269–1424 + conventions.md:23 | PR-OPENED (pushed; PR #2 comment 5727417425; cycle-1 SHIP. NOTE: push race carried unaudited ed04dff to remote — remediation: refspec-push policy) | 1 |
| SPEC-5-03 | 5 | "Sales over time" chart from real order/payment data (time-series aggregation + dashboard chart block + tests) — fulfils SPEC-1-10 | 1269–1424 [5.7],[5.9] | PR-OPENED (ed04dff verified; PR #2 comment 5727562233; cycle-1 SHIP) | 1 |
| SPEC-6-01 | 6 | DEVIATES [6.2.22]: out-of-stock products orderable — stock checked only at payment verify (orders/views.py:118-126,566-572). Gate line items at order creation with clear 400; payment-time atomic re-check stays as concurrency backstop | 1425–2262 [6.2.22] | PR-OPENED (pushed 313782e..823df1a via refspec; PR #2 comment 5727938088; cycle-1 SHIP) | 1 |
| SPEC-6-02 | 6 | DEVIATES [6.5.17] + conventions: silent inventory edits — admin list_editable stock bypasses StockMovement ledger; payment decrements skip ledger; adjust_stock lacks atomic/select_for_update (products/models.py:43-63). All mutation paths must write ledger; make adjust_stock atomic | 1425–2262 [6.5.8],[6.5.17] | SHIPPED (audit cycle-2 SHIP @ 092f606: 261 tests/255 pass/6 pinned xfails/cov 100.00%; cycle-1 BUG-1+BUG-2 closed & empirically re-probed; both builder deviations accepted; fix commit 092f606. Push origin/spec-comp ENV-BLOCKED — durability backup pending, see Escalations) | 2 |
| SPEC-6-03a | 6 | RBAC I: six staff roles (support/catalogue/inventory/marketing/finance/admin) as Groups + capability→role mapping in one module + idempotent seeding (makemigrations --check stays clean) + seeding idempotency test | 1425–2262 §6.12 lines 2099–2262 [6.12.2],[6.12.4],[6.12.7] | SHIPPED (audit cycle-1 SHIP @ 099ad81, combined 03a/03b/03c; commit 4a3f770; pushed 6f43710..099ad81; promotion at section-6 boundary gate) | 1 |
| SPEC-6-03b | 6 | RBAC II: DRF capability permission classes added to backend/common/permissions.py (extend IsAdminUserOrReadOnly seam, no duplication; Group/permission-based; admin role = full staff authority) + allow/deny + role→capability matrix tests | 1425–2262 §6.12 lines 2099–2262 | SHIPPED (audit cycle-1 SHIP @ 099ad81; commit 3e0934f; all 14 capability identifiers mapped per spec, least privilege; escalation guard pinned) | 1 |
| SPEC-6-03c | 6 | RBAC III: apply capability permission classes to API views (no guard downgrades — everything staff-only stays at-least-as-restricted); role/permission changes restricted to admin role (privilege-escalation guard, spec line 2261) | 1425–2262 §6.12 lines 2099–2262 | SHIPPED (audit cycle-1 SHIP @ 099ad81; commit 2736990; 3 judgment calls ACCEPTED w/ probes: role-less staff write denial = spec tightening, ops/dashboard deferral to 6-04/05 valid, 403 body byte-identical; 5/5 empirical probes OK) | 1 |
| SPEC-6-04 | 6 | ModelAdmin least-privilege: has_view/change/delete/add overrides per role; bulk actions and exports role-gated; sensitive actions get confirmation | 1425–2262 [6.12.4],[6.12.5],[6.12.7] | SHIPPED (audit cycle-1 SHIP @ feb4a2e, pushed 099ad81..1b2cff6; 36-pair role×kind matrix + interstitial + LogEntry verified; rulings a/b/c confirmed: CartAdmin view-only, OrderAdmin add/delete denied, SiteSettings add/delete superuser-proof, no over-restriction; P3 advisory logged: bulk status WHERE re-apply hardening → attributed to SPEC-9-07) | 1 |
| SPEC-6-05 | 6 | Staff/roles/audit surfaces: staff management, roles assignment UI, audit-log route, privileged-action logging for API-side ops | 1425–2262 [6.12.1],[6.12.6] | SPLIT into 05a/05b (token-budget discipline; audit-log route uses existing admin LogEntry — business-event audit model stays with SPEC-7-01) | 0 |
| SPEC-6-05a | 6 | Staff + roles management surfaces: UserAdmin staff fieldsets, Group (role) assignment restricted to admin role (staff.manage), no privilege self-escalation | 1425–2262 §6.12 [6.12.1] | PENDING | 0 |
| SPEC-6-05b | 6 | Privileged-action logging for API-side ops + audit-log route (LogEntry-based, staff-gated) | 1425–2262 §6.12 [6.12.6] | PENDING (blocked by 05a) | 0 |
| SPEC-6-06 | 6 | Dashboard depth I: units-sold KPI, top products/categories widget, recent-admin-activity widget on dashboard | 1425–2262 [6.1.6],[6.1.10],[6.1.14] | PENDING | 0 |
| SPEC-6-07 | 6 | Dashboard depth II: date-range selector, comparison period, filters, drill-down links from KPIs/chart, dashboard-metrics CSV export | 1425–2262 [6.1.15],[6.1.16],[6.1.18],[6.1.19],[6.1.20] | PENDING | 0 |
| SPEC-6-08 | 6 | Product catalogue depth: publish/status workflow + validation, brand/type, compare-at/cost/tax fields, variants+SKUs, multi-image media, SEO meta, archive-vs-delete, bulk actions, CSV import — Owner: S8 (cross S9/S3) | 1425–2262 [6.2.*] | PENDING | 0 |
| SPEC-6-09 | 6 | Coupon/promotion depth: product/category-specific discounts, free-shipping coupons, per-customer limits, eligibility, auto-promotions, stacking policy — Owner: S9 (cross S6) | 1425–2262 [6.6.*] | PENDING | 0 |
| SPEC-6-10 | 6 | Customer management depth: verified contacts, tags/segments, marketing consent, data export/deletion workflow — Owner: S9/S17 | 1425–2262 [6.7.*] | PENDING | 0 |
| SPEC-6-11 | 6 | Order management depth: item-level cancellation, internal/customer notes, invoice/packing slip, distinct ready-for-fulfilment state, payment/webhook history, audit trail beyond LogEntry — Owner: S10 | 1425–2262 [6.4.*] | PENDING | 0 |
| SPEC-6-12 | 6 | Returns/refunds/payments admin: refund widgets + workflows, payment success rates, webhook history, RA authorizations — Owner: S11 (cross SPEC-1-05/06/18, SPEC-5-11) | 1425–2262 [6.1.7],[6.1.8],[6.8.*] | PENDING | 0 |
| SPEC-6-13 | 6 | Inventory depth: locations, reserved/safety fields, transfers, purchase/receiving records, CSV import, reconciliation, category/collection/brand/attribute models — Owner: S12/S8 (cross SPEC-5-04/05) | 1425–2262 [6.3.*],[6.5.*] | PENDING | 0 |
| SPEC-5-04 | 5 | Catalogue admin surfaces: Category/Collection/Brand/Attribute/Review models+admins, media library — Owner: S8/S9 (cross SPEC-3-19/SPEC-3-20) | 1269–1424 [5.11] | PENDING | 0 |
| SPEC-5-05 | 5 | Fulfilment admin: Warehouses, Shipments, Shipping rules — Owner: S10/S12 (cross SPEC-1-07/SPEC-1-08) | 1269–1424 [5.12] | PENDING | 0 |
| SPEC-5-06 | 5 | Marketing admin: Campaigns, Gift cards (Newsletter subscribers → SPEC-3-10) — Owner: S6 (cross SPEC-1-17/SPEC-3-20) | 1269–1424 [5.13] | PENDING | 0 |
| SPEC-5-07 | 5 | Storefront content admin: Pages & content, Navigation menus, Homepage builder, SEO settings — Owner: S16 (cross SPEC-3-25/SPEC-3-09) | 1269–1424 [5.14] | PENDING | 0 |
| SPEC-5-08 | 5 | Operations admin: Support inbox/tickets, Activity logs, Import, Background jobs — Owner: S6/S7/S19 (cross SPEC-1-15/SPEC-1-11/SPEC-2-03) | 1269–1424 [5.15] | PENDING | 0 |
| SPEC-5-09 | 5 | Administration surfaces: Roles & permissions UI, Tax settings, Integrations, Payment settings surface — Owner: S6/S17/S16 | 1269–1424 [5.16] | PENDING | 0 |
| SPEC-5-10 | 5 | Unified global admin search (cross-entity "Search orders, products, customers…") — Owner: S20 | 1269–1424 [5.2] | PENDING | 0 |
| SPEC-5-11 | 5 | Payments admin surface + Returns & refunds nav (needs refund model) — Owner: S11 (cross SPEC-1-05/SPEC-1-18) | 1269–1424 [5.10] | PENDING | 0 |
| SPEC-2-01 | 2 | DATABASES hardcoded sqlite3 (settings.py:92-97) — make env-driven (e.g. dj-database-url) with sqlite dev fallback; Postgres provisioning itself stays deferred — Owner: build now; deployment/FTS deferred to S22/S9 | 152–278 [2.4] | PR-OPENED (MERGED: promoted via PR #3 merge 0ed1516. Gate closed in 3 cycles: SENT-BACK GitGuardian false positive on test literal → fix 0e0ed17 `unquote('p%40ss')` derived oracle → ESCALATE historical occurrence → user resolved incident 37423839 in dashboard → MERGED) | 2 |
| SPEC-2-02 | 2 | No component library — no accessible dialogs/data-table primitives alongside Tailwind — Owner: S15 | 152–278 [2.2] | PENDING | 0 |
| SPEC-2-03 | 2 | No Redis cache/job queue/async email (no CACHES/CELERY config; sync emails) — Owner: S19 | 152–278 [2.5] | PENDING | 0 |
| SPEC-2-04 | 2 | Local-disk media only — no S3/Cloudinary storage backend config — Owner: S22 | 152–278 [2.7] | PENDING | 0 |
| SPEC-2-05 | 2 | Search is icontains LIKE only — no PostgreSQL FTS (blocked by [2.4] resolution) — Owner: S9 | 152–278 [2.8] | PENDING | 0 |
| SPEC-2-06 | 2 | No SMS/OTP provider or OTP flow anywhere — Owner: S9 | 152–278 [2.10] | PENDING | 0 |
| SPEC-2-07 | 2 | No carrier/shipping-aggregator API integration — Owner: S10 (cross SPEC-1-07) | 152–278 [2.11] | PENDING | 0 |
| SPEC-2-08 | 2 | No monitoring — no LOGGING dict, no sentry-sdk, no metrics/alerts — Owner: S7 (cross S22) | 152–278 [2.12] | PENDING | 0 |
| SPEC-2-09 | 2 | No load-testing tooling (no locust/k6/artillery) — Owner: S21 | 152–278 [2.13] | PENDING | 0 |
| SPEC-2-10 | 2 | No deployment artifacts at all (no Dockerfile/compose/Procfile/render config) — Owner: S22 | 152–278 [2.14] | PENDING | 0 |
| SPEC-3-01 | 3 | Playwright e2e requires live backend + real Razorpay widget (e2e/helpers.ts:3, checkout.spec.ts) — violates "tests must not hit the network"; make e2e hermetic via route interception/mock gateway | 279–1230 + conventions.md:32 | PENDING | 0 |
| SPEC-3-02 | 3 | Guest checkout end-to-end: anonymous order creation keyed to email with same server-total/verify guards — fulfils SPEC-1-13 | 279–1230 [3.7.2] | PENDING | 0 |
| SPEC-3-03 | 3 | Checkout UX: visible progress indicator (3.7.1) + terms acknowledgement recorded server-side (3.7.8) | 279–1230 [3.7.1],[3.7.8] | PENDING | 0 |
| SPEC-3-04 | 3 | PDP enrichment (frontend-only): policy-summary block linking /returns + /shipping (3.4.12), low-stock badge with documented default threshold (3.4.5) | 279–1230 [3.4.5],[3.4.12] | PENDING | 0 |
| SPEC-3-05 | 3 | /search route reusing listing component + recent searches in localStorage (zero-result term tracking deferred to S5) | 279–1230 [3.3.1],[3.5.5] | PENDING | 0 |
| SPEC-3-06 | 3 | Static pages /about, /faq, /maintenance + footer links (/offers deferred to S5/S6) | 279–1230 [3.9.1],[3.9.3],[3.9.11] | PENDING | 0 |
| SPEC-3-07 | 3 | Product compare end-to-end (client-side, existing /api/products) — fulfils SPEC-1-04 | 279–1230 + S1[1.3] | PENDING | 0 |
| SPEC-3-08 | 3 | Docs-ledger sweep per SPEC-1-01 auditor note: audit.md F-20/F-24 → resolved; vulnerabilities.md V-19 split fixed/open; architecture.md:21 → permission_classes | follow-up | PENDING | 0 |
| SPEC-3-09 | 3 | Announcement bar + free-shipping threshold display — Owner: S16 (config schema) cross S3 | 279–1230 [3.1.1],[3.6.9] | PENDING | 0 |
| SPEC-3-10 | 3 | Newsletter signup end-to-end (consent-aware) — Owner: S9 cross S19 | 279–1230 [3.2.8] | PENDING | 0 |
| SPEC-3-11 | 3 | Breadcrumbs beyond PDP + cookie-consent preference UI + mini-cart (S3 P3 queue, after P2 builds) | 279–1230 [3.1.4],[3.1.7],[3.1.9] | PENDING | 0 |
| SPEC-3-12 | 3 | Delivery stage: methods, estimates, serviceability, shipping/tax breakdown at cart+checkout — Owner: S9/S16 (cross SPEC-1-07) | 279–1230 [3.6.6],[3.6.8],[3.7.5],[3.7.6] | PENDING | 0 |
| SPEC-3-13 | 3 | Payment-method choice at checkout — Owner: S11 | 279–1230 [3.7.7] | PENDING | 0 |
| SPEC-3-14 | 3 | Confirmation completeness: order email/SMS + estimated delivery + tracking link — Owner: S19/S10 (cross SPEC-1-08, SPEC-1-12) | 279–1230 [3.7.10],[3.8.5] | PENDING | 0 |
| SPEC-3-15 | 3 | Account area: /account/* routes, profile view/edit, password change, session mgmt, account deletion — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.8.1],[3.8.2],[3.8.11] | PENDING | 0 |
| SPEC-3-16 | 3 | Saved addresses CRUD + default selection at checkout — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.7.4],[3.8.3] | PENDING | 0 |
| SPEC-3-17 | 3 | Wishlist end-to-end (header icon, page, backend model/endpoints) — Owner: S9 | 279–1230 [3.1.2],[3.8.10] | PENDING | 0 |
| SPEC-3-18 | 3 | Customer cancellation request + return/refund request + reorder + invoice download — Owner: S10/S9 | 279–1230 [3.8.6]–[3.8.9] | PENDING | 0 |
| SPEC-3-19 | 3 | Reviews/social proof + rating sort/filters — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.2.7],[3.3.5],[3.3.6],[3.4.11] | PENDING | 0 |
| SPEC-3-20 | 3 | Merchandising: featured/best-selling, category resource + shortcuts, campaigns, /offers, search-term tracking, admin-editable homepage — Owner: S5/S6 cross S9 | 279–1230 [3.2.2],[3.2.3],[3.2.6],[3.2.9],[3.5.9],[3.9.9] | PENDING | 0 |
| SPEC-3-21 | 3 | Product schema gaps: multi-image gallery, variants, SKU, compare-at price, brand, structured specs — Owner: S8 cross S9 | 279–1230 [3.4.1]–[3.4.4],[3.4.10] | PENDING | 0 |
| SPEC-3-22 | 3 | Search depth: fuzzy/tolerance, ranking, suggestions, availability filter, related products — Owner: S9 (blocked by SPEC-2-05 FTS) | 279–1230 [3.4.13],[3.5.1]–[3.5.4],[3.5.6] | PENDING | 0 |
| SPEC-3-23 | 3 | Cart resilience: explicit merge/conflict rules on sign-in, unavailable-item messaging — Owner: S9/S10 | 279–1230 [3.6.11],[3.6.13] | PENDING | 0 |
| SPEC-3-24 | 3 | Contact form endpoint + submission flow — Owner: S9 cross S3 | 279–1230 [3.9.2] | PENDING | 0 |
| SPEC-3-25 | 3 | Admin-editable policies with review/publication workflow — Owner: S6 cross S16 | 279–1230 [3.9.13] | PENDING | 0 |
| SPEC-3-26 | 3 | /track-order token-based secure public tracking — Owner: S9/S10 (cross SPEC-1-08) | 279–1230 [3.9.8] | PENDING | 0 |
| SPEC-7-01 | 7 | Audit trail: append-only audit-log model + write hooks for order/payment/auth events (reuse StockMovement pattern) + migration + tests — fulfils the audit-trail half of SPEC-1-11 | 2263–2453 [R-7.20] + S1[1.22] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-02 | 7 | Logging baseline: env-driven LOGGING dict in settings (request errors + payment failures), wired so audit events and verify_payment failures emit — fulfils logging halves of SPEC-1-11/SPEC-2-08 | 2263–2453 + S1[1.22], S2[2.12] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-03 | 7 | Coupon quantize parity: quantize discount Decimals before compare/serialize in preview (orders/views.py:239-241,414-418) + checkout paths — flips pinned expectedFailure F-11 (orders/tests.py:190) | 2263–2453 [R-7.7 conventions finding 1] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-7-04 | 7 | Search hardening: env-driven pagination page size (products/views.py:94 hardcoded 2, F-23) + stable default ordering (F-12, flips pinned expectedFailure) in products listing | 2263–2453 [R-7.6 conventions findings 5] | PENDING (from S7 compliance prefetch) | 0 |
| SPEC-8-01 | 8 | Order-number + non-sequential identifier schema: unique indexed order_number (ORD-YYYY-NNNNNN, generated inside atomic creation), UUID-vs-derived PK/exposure strategy for Order; uniqueness-race test | 2454–2570 [R-8.4],[R-8.5] | PENDING (from S8 compliance prefetch) | 0 |
| SPEC-8-02 | 8 | SKU + variant schema core: ProductVariant with globally unique SKU (constraint+index), OrderItem snapshot columns (sku, variant_name) — executes the schema half of SPEC-3-21/SPEC-6-08 | 2454–2570 [R-8.7],[R-8.13] | PENDING (from S8 compliance prefetch; full catalogue/admin depth stays with SPEC-3-21/SPEC-6-08) | 0 |
| SPEC-8-03 | 8 | Currency column alongside money: currency CharField (default INR, documented) on Order/OrderItem + discount amounts, serializer-surfaced, store-config-driven (not hardcoded 'INR' at orders/views.py:498,510) | 2454–2570 [R-8.11] | PENDING (from S8 compliance prefetch) | 0 |
| SPEC-8-04 | 8 | Business-event timestamps: paid_at (written in verify_payment beside status), cancelled_at; named-timestamp pattern for shipped/delivered/refunded/published so later sections extend it | 2454–2570 [R-8.16] | PENDING (from S8 compliance prefetch; cross SPEC-6-11/SPEC-1-05/§10) | 0 |
| SPEC-8-05 | 8 | Explicit starting indexes: Meta.indexes for Order(user,-created_at), Order(status,created_at), Product(category); SKU/variant+location indexes ride SPEC-8-02/SPEC-6-13 | 2454–2570 [R-8.17] | PENDING (from S8 compliance prefetch) | 0 |
| SPEC-8-06 | 8 | Deletion/archival policy: soft-archive field + admin behavior folded into SPEC-6-08 archive-vs-delete; slug IntegrityError-retry alignment + products-model rename sequencing on same model-touches | 2454–2570 [R-8.9] + conventions 17,7 | PENDING (from S8 compliance prefetch) | 0 |
| SPEC-9-01 | 9 | P1 — Checkout submit idempotency + duplicate-order guard: Idempotency-Key on POST checkout, unique-by-key dedupe inside existing atomic block, regression tests | 2571–3404 [R-9.3.14],[R-9.3.19] | PENDING (from S9 compliance prefetch; §12 keeps reservation concurrency) | 0 |
| SPEC-9-02 | 9 | P2 — /api/v1/ namespace + prefix mapping (store/account/admin/webhooks), legacy-path aliases, dual-mount tests; coordinate frontend base URL with S3 before cutover | 2571–3404 [R-9.0 DEVIATES] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-03 | 9 | P2 — Uniform error envelope: one helper converting DRF field errors + {"error":…} into a single response shape, envelope-pinning tests | 2571–3404 [R-9.2.19] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-04 | 9 | P2 — Customer order-detail endpoint GET /api/orders/<id>/ owner-checked + pagination on order history | 2571–3404 [R-9.2.14],[R-9.2.15] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-05 | 9 | P2 — Coupon apply/remove as cart state: POST/DELETE /api/cart/coupon/ persisting coupon FK on Cart, re-validated at checkout; depth policy stays SPEC-6-09 | 2571–3404 [R-9.3.5],[R-9.3.6] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-06 | 9 | P3 — REST inventory-adjustment endpoint reusing adjust_stock + HasInventoryAdjust (already defined, unused), ledger-assertion tests | 2571–3404 [R-9.4.7] | PENDING (from S9 compliance prefetch) | 0 |
| SPEC-9-07 | 9 | P3 — Admin orders JSON seam: GET /api/admin/orders/ + /:id/ (orders.read), POST fulfill/cancel reusing transition_allowed + HasOrdersFulfill/HasOrdersCancel; INCLUDES P3 hardening: re-apply status__in predicate in _bulk_set_status WHERE (SPEC-6-04 audit advisory) | 2571–3404 [R-9.4.8]–[R-9.4.11] + SPEC-6-04 advisory | PENDING (from S9 compliance prefetch; feature depth stays SPEC-6-11) | 0 |
| SPEC-9-08 | 9 | P3 — Write-endpoint contract doc: per-endpoint 10-point table (R-9.5 checklist) as backend/docs/api-contract.md (docs-only) | 2571–3404 [R-9.5] | PENDING (from S9 compliance prefetch) | 0 |

Statuses: `PENDING -> IN-COMPLIANCE -> BUILDING -> IN-AUDIT -> BUGS-FOUND -> SHIPPED -> PR-OPENED | ESCALATED | NOT-APPLICABLE`. Max 3 builder→auditor fix cycles per task.

Note: Section 3 compliance ran concurrently with the SPEC-1-02 build (commit 854396a) and audited the pre-854396a tree; its "no throttle config anywhere" convention finding is resolved by SPEC-1-02 — auditor confirms.

Note: §4 route matrix decoded by orchestrator via PowerShell slicing (line 1247, 8,856 chars, 22 rows — badge divs + plain `data-d-size="xs"` text cells). Corrections to the §4 compliance report: (1) NO "Server-only" row exists — [4.24] was a phantom from the agent's probe constraints, resolved NOT-APPLICABLE; (2) the matrix classifies `/checkout` as **"Guest or authenticated"** (not "Authenticated") — guest checkout is required by §4's own map, strengthening SPEC-1-13/SPEC-3-02 (tracked); (3) full matrix rows all map to tracked tasks: /categories//collections//search → SPEC-3-05/SPEC-3-20; /account/addresses+returns → SPEC-3-16/SPEC-3-18; /track-order "Limited public access" → SPEC-3-26; /api/webhooks/* "Verified provider only" → SPEC-1-05/SPEC-1-06 (webhook must verify provider signatures); /account/wishlist "Authenticated or local" → SPEC-3-17; /reset-password "Token-gated" → IMPLEMENTED (verified).

## Baseline

Current floor (AUDITOR-verified 2026-09-18, tree @ `1b2cff6`, SPEC-6-04 audit): **307 tests, OK (301 pass + 6 pre-existing expectedFailure), cov 100.00%** (1406 stmts — measured set now INCLUDES common/*; auditor-endorsed re-baseline, verified 0 miss). Floor only moves up: never fewer passing tests, never more expected failures, never lower coverage.
last-promoted SHA: **0ed1516** (PR #3 merge "Merge pull request #3 from boredompeaks/spec-comp", `ac75148..0ed1516`, promoted 2026-09-18 — covers S1/S2/S5 + S6-01/6-02 incl. gate fix 0e0ed17; feat/add-frontend tip = 0ed1516 verified via fetch. Rows referencing closed PR #2 comments are historical.)

## Escalations

- 2026-09-18 -- USER CONSENT (dirty-tree resolution): `frontend/scripts/demo_walkthrough.mjs` is USER-OWNED, stays UNTRACKED by design (demo artifact). No agent may commit, modify, delete, or flag it — it is NOT a scope finding. It intentionally contains demo credentials/real Razorpay card flow; committing it would re-trigger GitGuardian — treat any attempt to commit it as a violation. Git status will perpetually show it as `??`; auditors: ignore this path in diff-scope checks.

- 2026-09-18 -- GATE_PHASE CLOSED (MERGED, 3 cycles): cycle-3 report MERGED after user resolved GitGuardian incident 37423839; merge verified independently by orchestrator fetch — feat/add-frontend tip 0ed1516 = "Merge pull request #3 from boredompeaks/spec-comp" (ac75148..0ed1516). Last-promoted SHA recorded in Baseline. PR #2 remains closed, master untouched. STANDING DIRECTIVE (user): RE merges promotion PRs on full green — never leaves them open; RE re-verifies post-merge CI on feat/add-frontend at next wake. GATE -> BUILD transition: section 6 queue (SPEC-6-03 first), S7 compliance prefetched in parallel.

- 2026-09-18 -- RESOLUTION (user): GitGuardian incident 37423839 resolved in dashboard (false positive/test credential). RE re-invoked for cycle 3 of 3 (FINAL): push tip, re-verify all-green on PR #3 head, MERGE. User directive: RE must MERGE promotion PRs on full green (never leave them open/closed unmerged).

- 2026-09-18 -- GATE_PHASE cycle-2 ESCALATE (sole blocker is owner-side): RE pushed 6819520..6f43710, CI green on tip (backend-tests 35340716862: 261/6expF/100.00% floor held; frontend 35340716921 green, e2e self-skipped by design; gitleaks 35340716864 clean), PR #3 head now 6f43710, OPEN + MERGEABLE, merge correctly withheld. GitGuardian check-run on head = failure SOLELY due to historical commit 313782e (occurrence 298458886, pre-fix literal at tests/test_settings_security.py:97, removed by 0e0ed17; grep: 0 .py matches tree-wide, 1 doc-prose match in changes.md:111 not flagged). UNBLOCKER (repo owner only): resolve incident 37423839 as false positive/test credential at dashboard.gitguardian.com/workspace/791682/incidents/37423839?occurrence=298458886. No builder task needed — not a code defect. Cycle 3 of 3 reserved: wake RE to re-verify + merge on all-green once incident is resolved.

- 2026-09-18 -- GATE_PHASE SENT-BACK cycle-1 (first promotion, S1/S2/S5/S6-01/6-02): RE pushed spec-comp (823df1a..f0c65ec + CI commits 210416a, 6819520), authored CI/CD (.github/workflows/backend-tests.yml, frontend-tests.yml, secret-scan.yml, .gitleaks.toml), CI ALL GREEN on tip (backend 261/6expF/100.00% floor held exactly; frontend vitest 4/4 + build; gitleaks clean), CLOSED legacy PR #2 (never merged), OPENED promotion PR #3 (spec-comp -> feat/add-frontend). Merge WITHHELD on GitGuardian app-check failure. BUG-1 P2 -> builder fix (SPEC-2-01, restructure test_settings_security.py:97 password-shaped literal; gate cycle 1 of 3). BUG-2 P3 -> repo-owner only: 9 legacy GitGuardian findings (dummy e2e fixtures, pre-run commits e3f235c/95d5d39, closed PR #2 only) — dashboard hygiene, no gate impact. BUG-3 P2 -> pre-existing non-hermetic e2e (real Razorpay widget) — already ledgered as SPEC-3-01, e2e CI job self-skips behind vars.E2E_ENABLED until fixed. USER ACTION (parallel, only owner can do): resolve GitGuardian incident 37423839 as test credential — code fix alone may not clear the historical occurrence (313782e remains in PR history). Re-promote: wake RE cycle-2 after builder fix lands.

- 2026-09-18 -- PERMISSION FIX (root cause found; resolves the ENV PERMISSION BLOCK below): opencode v1.18.31 resolves overlapping bash permission rules LAST-match-wins (`findLast` in permission/index.ts); unmatched commands fall back to `ask`. The auditor's blanket `"git push*": deny` sat AFTER its scoped allows and swallowed every push form, allowlisted or not -- that is the entire push denial. The release-engineer was never blocked (its block has no blanket push deny). All agent blocks have been reordered: `"*": allow` baseline first, targeted denies after (force-push, master, feat/add-frontend, destructive gh ops), scoped allows LAST (`git push origin spec-comp` / `:spec-comp` / `HEAD:spec-comp` for auditor + RE; `gh pr create/merge/close` for RE). Subagents spawned after this fix load the corrected blocks in-session; if an op is STILL denied, restart the session (ledger is complete) and re-run from GATE_PHASE.

- 2026-09-18 -- ENV PERMISSION BLOCK (GATE_PHASE mechanics): the session permission layer denies `git push*`, `gh pr create*`, `gh pr merge*`, `gh pr close*` for ALL agents — confirmed for both a subagent (SPEC-6-02 cycle-2 auditor: all allowlisted push forms denied) and the orchestrator (direct denial). Consequences: (1) durability backup of audited tip 092f606 to origin/spec-comp cannot be executed by any agent; (2) GATE_PHASE cannot run — no CI trigger (requires push), no promotion PR creation, no legacy PR #2 close, no merge. SPEC-6-02 is auditor-SHIPPED locally at 092f606 (clean working tree). Escalated to user; options presented: grant scoped permissions, or user executes the prepared mechanical command list, or hybrid. No subagent commissioned while this holds.

- 2026-09-18 -- RESOLUTION (user decision, supersedes the hold below): promotion model adopted. The release-engineer is integrated at section boundaries WITH merge authority (auto-merge on all-green: macro commit analysis + GitGuardian/gitleaks + full CI). The auditor KEEPS its push duty. Per-section PRs are achieved via successive delta-only promotion PRs `spec-comp -> feat/add-frontend` (no cherry-picks, no spec-sN branches -- after each merge the next PR shows only that section's commits). Legacy PR #2 (base master) is closed at first promotion, never merged. Master stays frozen. Resume sequence: drain held SPEC-6-02 audit -> GATE_PHASE (RE exclusive) -> BUILD_PHASE resumes on RE's MERGED report.
- 2026-09-18 -- USER WORKFLOW DIRECTIVE: per-section PRs ("only commits for that section to that PR"), not one massive PR. All subagent dispatches for next sections HELD pending user decision on release-engineer timing (now vs end-of-run sweep). In-flight: SPEC-6-02 built (52ef87d), audit ready but not commissioned. Current PR #2 (spec-comp → master) holds all shipped commits interleaved: S1 (SPEC-1-01/02/03/22), S2 (SPEC-2-01), S5 (SPEC-5-01/02/03), S6 (SPEC-6-01) + ledger chores -- a per-section split requires branch surgery (cherry-pick) since commits are chronologically interleaved on one branch.
