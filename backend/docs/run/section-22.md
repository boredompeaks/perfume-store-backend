# Section 22 — Deployment and infrastructure (task ledger)

Spec lines 4938–5070. Created 2026-09-30 from the S22 compliance re-derivation (compliance subagent had no write/command tools; static sweep). Evidence: `backend/docs/compliance/S22.md`.

**36 rows — 6 IMPLEMENTED / 11 PARTIAL / 4 MISSING / 1 DEVIATES / 1 NOT-APPLICABLE / 13 DEFERRED.** The deployment-gap sink. Headline: a test-pinned production Postgres path that cannot connect (no psycopg) plus no deployable unit (no Dockerfile/compose/Procfile), no STATIC_ROOT, sslmode discarded. Prefetch expected order honored below, but the two P1s are split for sizing.

| Task ID | Requirement summary | Spec lines | Priority | Owner | Status | Attempts |
|---|---|---|---|---|---|---|
| SPEC-22-01 | Add psycopg + gunicorn + whitenoise; STATIC_ROOT/STORAGES; parse sslmode (and other query params) into OPTIONS; make the postgres path actually loadable + collectstatic-ready; update .env.example to match reality | R-22.5 | P1 (prod-breaking) | S22 | SHIPPED @ b319d0a (audit FULL SHIP: prod `sslmode=require` reaches real OPTIONS; no-query postgres omits OPTIONS; sqlite never gets OPTIONS; whitenoise ordered Security<whitenoise<Session, not DEBUG-gated; collectstatic 157 files under DEBUG=false; 12 new tests; deps importable; floor 1021→1033) | 1 |
| SPEC-2-10a | Container image: backend/frontend Dockerfile(s) + .dockerignore — the deployable unit, image half | R-22.5 | P1 | CROSS-SECTION->S2 (promoted here) | SHIPPED @ d64afa5 (audit FULL SHIP: non-root uid 10001, gunicorn --bind 0.0.0.0:${PORT}, collectstatic at build; placeholder SECRET_KEY is a non-persisted build-time prefix and fail-closed guard PROVEN intact; .dockerignore covers .env/db.sqlite3/venv/media/.git/staticfiles; Docker build not attempted — no daemon on host) | 1 |
| SPEC-22-02 | Backup schedule (pg_dump/managed) + retention + code/release rollback procedure (version pin, migration-down) — close the runbook's S22 deferral | R-22.9, R-22.23 | P1 | S22 | PENDING (batch 2) | 0 |
| SPEC-2-10b | Orchestration half: docker-compose + Procfile + release-migrate step + deploy workflow | R-22.5, R-22.11 | P2 | CROSS-SECTION->S2 (promoted here) | PENDING (batch 2) | 0 |
| SPEC-22-03 | Staging env + per-env credential/data-isolation provisioning + refuse silent sqlite fallback on a prod DATABASE_URL | R-22.1, R-22.2, R-22.3 | P2 | S22 | PENDING (batch 2) | 0 |
| SPEC-22-07 | Release-time migrate step + migration-review checkpoint (R-22.11 has pre-merge gate only) | R-22.11 | P2 | S22 | PENDING (batch 2) | 0 |
| SPEC-2-04 | Real production media/static serving (STORAGES backend + urls.py; fix static() silent no-op when DEBUG=False, V-13) — CONFIG-ONLY, no storage model | R-22.5 | P2 | CROSS-SECTION->S2 (promoted here) | PENDING (batch 2) | 0 |
| SPEC-22-04 | Env-gated error tracking (sentry SDK + DSN) + uptime registration of /health/ | R-22.13, R-22.12 | P2 | S22 | PENDING (batch 3) | 0 |
| SPEC-22-06 | WAF ruleset + secret-manager/rotation story (the unowned halves of R-22.16 WAF / R-22.8) | 5024, 5008 | P2 | S22 | PENDING (batch 3) | 0 |
| SPEC-22-08 | Enable the shipped hardening in the deploy artifact: SECURE_SSL_REDIRECT + HSTS + trusted proxy header (SPEC-17-07 half shipped, V-06) | R-22.6 | P2 | S22 | PENDING (batch 3) | 0 |
| SPEC-22-09 | Controlled process for real customer data in non-prod environments | R-22.4 | P3 | S22 | PENDING (batch 4) | 0 |
| SPEC-22-10 | Email delivery verification (send-probe or SPF/DKIM/DMARC guidance) | R-22.18 | P3 | S22 | PENDING (batch 4) | 0 |
| SPEC-22-11 | Pin all CI actions by commit SHA (supply-chain hardening) | CI config | P3 | S22 | PENDING (batch 4) | 0 |
| SPEC-22-05 | .gitguardian.yml filename on the default branch | CI config | P3 | S22 (OWNER action) | PENDING — master frozen for agents; owner applies on master (batch 4/owner) | 0 |

CROSS-SECTION owners NOT rebuilt here (built in their own sections, re-verified in the final sweep): SPEC-1-06 (webhook + sig verify + dedup — P1), SPEC-2-08 (metrics/error-rate/latency/DB-sat/slow-query/funnel/severity-routing — owns R-22.14, .24-.28, .29-residual, .31, .32, .34, .35, .36-residual), SPEC-2-03 (outbox/jobs — R-22.33), SPEC-14-1 (production seed — R-22.20), SPEC-18-33/22 (CDN — R-22.16 CDN half). SPEC-2-10 and SPEC-2-04 are CROSS-SECTION->S2 but PROMOTED into this section's loop (they are the deployable unit for R-22.5) and split per the sizing gate.