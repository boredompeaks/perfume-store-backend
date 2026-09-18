# Section 18 — SEO, performance and discoverability (task ledger)

Created 2026-09-19 from the S18 compliance prefetch. Spec lines: 4427–4602. Prefetch result: 36 rows — 24 IMPLEMENTED / 5 PARTIAL / 5 MISSING / 0 DEVIATES / 2 N-A (vacuous-verified: category/collection indexing rules attach when those resources exist).

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-18-1 | 18 | MISSING [R-18.12]: slug-change 301 mechanism — SlugRedirect model (unique old_slug → product FK) written on slug mutation, resolved in product_detail (optionally checked by frontend PDP); files: products/models.py, views.py, admin.py, migration, tests (≤5) | 4427–4602 [R-18.12] | PENDING (P3; rides SPEC-8-06's slug/model-rename sequencing when touched) | 0 |
| SPEC-18-2 | 18 | MISSING [R-18.25/18.36]: no performance measurement — add Lighthouse CI mobile-preset budgets (LCP≤2.5s, CLS≤0.1, TBT as INP proxy) to the frontend-tests workflow + record targets in PLAN.md; files: frontend-tests.yml, lighthouserc, PLAN.md (≤3) | 4427–4602 [R-18.25],[R-18.36] | PENDING (P3; measurement half of CWV) | 0 |

Owner attributions (no duplicate tasks):
- R-18.11/R-18.23 (discontinued/unpublished product states) → **SPEC-6-08** (publish/status workflow).
- R-18.30 (hardcoded page size 2, unordered pagination queryset — also inflates the sitemap walk cost) → **SPEC-7-04** (in queue; flips F-12). Order-history pagination → **SPEC-9-04**.
- Hot-path indexes (Order user/status, Product category) → **SPEC-8-05**. Slug generation check-then-act → **SPEC-8-06** (rename sequencing).
- R-18.5 JSON-LD sink → **SPEC-17-06** (escape + CSP; S18 accounts for that fix). R-18.13/15/16 (category/collection link graph + indexing) → **SPEC-3-20 + SPEC-6-13**.
- R-18.29 backend cache layer → **SPEC-2-03** (S19). R-18.34 latency/query monitoring → **SPEC-2-08** (cross S22). R-18.22 API-origin robots/X-Robots-Tag + R-18.33 CDN assetPrefix → **S22** (deployment loop).
- Ownership regression test for R-18.24 → **SPEC-4-01**.

Verified strong (highlights): unique titles/descriptions, canonical + faceted-noindex handling, OG + Twitter cards, Product/Organization/BreadcrumbList JSON-LD, sitemap (products walked, revalidate 3600, graceful degrade) + robots (private set disallowed), noindex on all 11 private pages + admin redirect, next/image everywhere (zero raw img) with sizes + priority discipline, aspect-ratio CLS guards, display:swap fonts, server catalogue cache revalidate:60, admin tables paginated/ordered/filtered, ops select_related. No new P1/P2 findings.
