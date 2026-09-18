# Section 14 — Recommended backend project structure (task ledger)

Created 2026-09-19 from the S14 compliance prefetch. Spec lines: 3760–3854. Prefetch result: 17 rows — 10 IMPLEMENTED / 6 PARTIAL / 1 MISSING / 0 DEVIATES / 0 N-A. §14 lacks §13's illustrative-tree disclaimer and its reference tree is NestJS/TypeScript-shaped (impossible literally on Django) — portable substance weighted fully, file names directional.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-14-1 | 14 | MISSING [R-14.4 fraction]: zero seed/fixture/management-command code anywhere — add a demo-catalogue seed as a products management command or fixture (products/management/commands/seed_demo.py) + one test | 3760–3854 [R-14.4] | PENDING (P3, REAL gap; not build-now — admin-driven entry suffices today) | 0 |
| SPEC-14-2 | 14 | PARTIAL [R-14.6/R-14.11 fraction]: Razorpay client constructed inline twice (orders/views.py:498-503 vs 575-580) + email inline in accounts/views.py — extract a thin gateway/email seam (single client provider + shared _send_email home) when orders/payments or notifications code is next touched; behavior unchanged | 3760–3854 [R-14.6],[R-14.11] | PENDING (P3, COSMETIC align-when-touch per run precedent) | 0 |

Owner attributions (no duplicate tasks):
- R-14.3 content gaps: DATABASES env wiring → **SPEC-2-01** (delivered, PR #3); LOGGING block → **SPEC-7-02** (queued).
- R-14.7 jobs/ layer → **SPEC-2-03** (infra, Owner S19) + **SPEC-12-03** (expire_reservations command) + **SPEC-3-14** (notification delivery); exports half exists synchronously (products/admin.py, test-pinned).
- R-14.5 unbuilt domains → their feature owners: shipping→SPEC-2-07/SPEC-3-12, returns→SPEC-3-18, reviews→SPEC-3-19, cms→SPEC-3-20/3-25/3-06, categories resource→SPEC-3-20.
- R-14.15 events responsibility → **SPEC-7-01** (delivered: AuditEvent + StockMovement + LogEntry trails).

Grandfathered convention findings (align-when-touch, no drive-by rewrites): plural lowercase model name (conventions.md:7), multi-method function views vs CBVs (conventions.md:13), check-then-act slug generation vs IntegrityError retry (conventions.md:17, test-pinned as current behavior), config/urls.py formatting (conventions.md:43). Owned: stale architecture.md → SPEC-3-08; hardcoded pagination page size → SPEC-7-04.

Verified strong (highlights): acyclic import graph — common imports no app (Order FK is a string ref, common/models.py:88); payment path mutates ONLY its own order's related entities inside one atomic block with select_for_update on Order/Products/Coupon (orders/views.py:617-776); explicit transaction boundaries at every mutation surface; hermetic test base common/testing.py; per-app test density real. No P1/P2 findings.
