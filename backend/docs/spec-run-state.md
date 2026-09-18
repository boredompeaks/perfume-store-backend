# Spec run state

Source of truth for the spec-compliance run. Updated at every status transition with evidence (commit SHA, test command + result, or report verdict).

- Spec: `Saturday, Jul 25, 2026 at 5_23 AM.txt` (repo root, 5,734 lines; git-ignored by design)
- Work branch: `spec-comp` (created from `feat/add-frontend` @ `ac75148`); PR base: `master`; only the auditor pushes and opens/updates the PR. Orchestrator never merges.
- Conventions: `backend/docs/conventions.md` (quoted into every builder handoff)
- Changelog: `backend/docs/changes.md` (append-only rows; auditor verifies each row against the diff)

## Section index

| # | Title | Spec lines | Status |
|---|---|---|---|
| 1 | System overview | 29–151 | IN-COMPLIANCE |
| 2 | Recommended technology stack | 152–278 | IN-COMPLIANCE |
| 3 | Frontend — customer-facing website | 279–1230 | PENDING |
| 4 | Complete route map — what is public, gated or restricted? | 1231–1268 | PENDING |
| 5 | Admin panel — complete specification | 1269–1424 | PENDING |
| 6 | Admin features — detailed functional requirements | 1425–2262 | PENDING |
| 7 | Backend architecture | 2263–2453 | PENDING |
| 8 | Database architecture | 2454–2570 | PENDING |
| 9 | API design — the complete backend contract | 2571–3404 | PENDING |
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
| SPEC-1-02 | 1 | Public mutating endpoints need throttle scopes (apply_coupon, cart mutations, login/register) and anonymous coupon errors must be uniform — no coupon existence/validation-state leaks (orders/views.py:284-285) | 29–151 + conventions.md:24,25 | BUILDING (attempt 2; first dispatch interrupted, no partial work landed — tree verified clean at 5b88c43) | 0 |
| SPEC-1-03 | 1 | Registration must run `validate_password` (same policy as reset; accounts/serializers.py:7-10 only enforces min_length=8) | 29–151 + conventions.md:19 | PENDING | 0 |
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
| SPEC-2-01 | 2 | DATABASES hardcoded sqlite3 (settings.py:92-97) — make env-driven (e.g. dj-database-url) with sqlite dev fallback; Postgres provisioning itself stays deferred — Owner: build now; deployment/FTS deferred to S22/S9 | 152–278 [2.4] | PENDING | 0 |
| SPEC-2-02 | 2 | No component library — no accessible dialogs/data-table primitives alongside Tailwind — Owner: S15 | 152–278 [2.2] | PENDING | 0 |
| SPEC-2-03 | 2 | No Redis cache/job queue/async email (no CACHES/CELERY config; sync emails) — Owner: S19 | 152–278 [2.5] | PENDING | 0 |
| SPEC-2-04 | 2 | Local-disk media only — no S3/Cloudinary storage backend config — Owner: S22 | 152–278 [2.7] | PENDING | 0 |
| SPEC-2-05 | 2 | Search is icontains LIKE only — no PostgreSQL FTS (blocked by [2.4] resolution) — Owner: S9 | 152–278 [2.8] | PENDING | 0 |
| SPEC-2-06 | 2 | No SMS/OTP provider or OTP flow anywhere — Owner: S9 | 152–278 [2.10] | PENDING | 0 |
| SPEC-2-07 | 2 | No carrier/shipping-aggregator API integration — Owner: S10 (cross SPEC-1-07) | 152–278 [2.11] | PENDING | 0 |
| SPEC-2-08 | 2 | No monitoring — no LOGGING dict, no sentry-sdk, no metrics/alerts — Owner: S7 (cross S22) | 152–278 [2.12] | PENDING | 0 |
| SPEC-2-09 | 2 | No load-testing tooling (no locust/k6/artillery) — Owner: S21 | 152–278 [2.13] | PENDING | 0 |
| SPEC-2-10 | 2 | No deployment artifacts at all (no Dockerfile/compose/Procfile/render config) — Owner: S22 | 152–278 [2.14] | PENDING | 0 |

Statuses: `PENDING -> IN-COMPLIANCE -> BUILDING -> IN-AUDIT -> BUGS-FOUND -> SHIPPED -> PR-OPENED | ESCALATED | NOT-APPLICABLE`. Max 3 builder→auditor fix cycles per task.

## Baseline

backend tests: 178 pass, cov 100.00% (2026-09-18)
Current floor after SPEC-1-01 (PR #2): 186 pass + 8 pre-existing expected failures, cov 100.00% (auditor-verified incl. baseline worktree re-run)

## Escalations
