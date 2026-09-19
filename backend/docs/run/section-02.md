# Section 2 — Recommended technology stack (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 152–278.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-2-01 | 2 | DATABASES hardcoded sqlite3 (settings.py:92-97) — make env-driven (e.g. dj-database-url) with sqlite dev fallback; Postgres provisioning itself stays deferred — Owner: build now; deployment/FTS deferred to S22/S9 | 152–278 [2.4] | PR-OPENED (MERGED: promoted via PR #3 merge 0ed1516. Gate closed in 3 cycles: SENT-BACK GitGuardian false positive on test literal → fix 0e0ed17 `unquote('p%40ss')` derived oracle → ESCALATE historical occurrence → user resolved incident 37423839 in dashboard → MERGED) | 2 |
| SPEC-2-02 | 2 | No component library — no accessible dialogs/data-table primitives alongside Tailwind — Owner: S15 | 152–278 [2.2] | PENDING (2026-09-19 annotation: the inputClass centralization + AA field styling slice was pulled forward and delivered by SPEC-15-1 (src/lib/ui.ts + line-strong token); the broader primitives scope — dialogs, data tables, Alert/EmptyState/ErrorState extraction, tokens.ts naming — remains here) | 0 |
| SPEC-2-03 | 2 | No Redis cache/job queue/async email (no CACHES/CELERY config; sync emails) — Owner: S19 | 152–278 [2.5] | PENDING | 0 |
| SPEC-2-04 | 2 | Local-disk media only — no S3/Cloudinary storage backend config — Owner: S22 | 152–278 [2.7] | PENDING | 0 |
| SPEC-2-05 | 2 | Search is icontains LIKE only — no PostgreSQL FTS (blocked by [2.4] resolution) — Owner: S9 | 152–278 [2.8] | PENDING | 0 |
| SPEC-2-06 | 2 | No SMS/OTP provider or OTP flow anywhere — Owner: S9 | 152–278 [2.10] | PENDING | 0 |
| SPEC-2-07 | 2 | No carrier/shipping-aggregator API integration — Owner: S10 (cross SPEC-1-07) | 152–278 [2.11] | PENDING | 0 |
| SPEC-2-08 | 2 | No monitoring — no LOGGING dict, no sentry-sdk, no metrics/alerts — Owner: S7 (cross S22) | 152–278 [2.12] | PENDING | 0 |
| SPEC-2-09 | 2 | No load-testing tooling (no locust/k6/artillery) — Owner: S21 | 152–278 [2.13] | PENDING | 0 |
| SPEC-2-10 | 2 | No deployment artifacts at all (no Dockerfile/compose/Procfile/render config) — Owner: S22 | 152–278 [2.14] | PENDING | 0 |
