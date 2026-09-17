# Changelog

Running log of repo-level changes. Keep updated with every commit that matters. (Source-code behavior is unchanged as of this entry — audit only.)

## 2026-09-17 / 2026-09-18 — Audit & repo bootstrap

- Created git repo; pushed base to `github.com/boredompeaks/perfume-store-backend` (private).
- Added `.gitignore` (excludes `.env`, `db.sqlite3`, `media/`, `venv/`, caches) — **no secrets in the repo**.
- Added pinned `requirements.txt` (Django 6.1.1, DRF 3.18.1, SimpleJWT 5.5.1, cors-headers 4.9.0, razorpay 2.0.1, python-dotenv 1.2.3, Pillow 12.3.0) — installed and verified in `venv/` (excluded from git).
- Added `.env.example` template.
- Wrote docs suite: `README.md`, `docs/architecture.md`, `docs/conventions.md`, `docs/audit.md` (29 findings, F-01..F-29), `docs/vulnerabilities.md` (21 flagged: 3 critical / 4 high / 8 medium / 6 low), `docs/test-gaps.md` (**0 unit + 0 e2e tests exist; 39 unit + 12 e2e enumerated**), `docs/fix-plan.md` (6 phases), this file.
- Verified existing code WITHOUT modifying it:
  - `manage.py check` — 0 issues
  - `makemigrations --check --dry-run` — no drift
  - `manage.py test` — 0 tests found
  - `pip check` — no conflicts
  - `check --deploy` — 5 security warnings (HSTS, SSL redirect, secret, session/CSRF cookies)
  - E2E smoke (temp script, in-memory DB): **20/20 passed** — full auth flows, staff gates, cart, coupon, checkout, live Razorpay order creation, forged-signature rejection, password reset.
- New findings surfaced at runtime (added to audit): discount rounding drift preview-vs-checkout (F-11), unordered pagination warning (F-12), live Razorpay keys confirmed valid (F-01/V-01 urgency).

## Next (per fix-plan.md)

- Phase 0 remaining: rotate Razorpay keys, add CI.
- Phase 1: webhook + reconciliation + refund path.
