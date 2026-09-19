# Section 16 — Store settings and configuration (task ledger)

Created 2026-09-19 from the S16 compliance prefetch. Spec lines: 4089–4238. Prefetch result: 22 rows — 5 IMPLEMENTED / 13 PARTIAL / 3 MISSING / 0 DEVIATES / 1 N-A (vacuous-verified).

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-16-1 | 16 | PARTIAL [R-16.3]: store name hardcoded in frontend site.ts (not merchant-editable); no logo field, no locale — add store identity (name/logo ImageField/locale) to SiteSettings + admin fieldsets + /api/settings/ payload + frontend fallback consumption; include server-settings.ts env-fallback test (currently untested) | 4089–4238 [R-16.3] | PENDING (P3) | 0 |
| SPEC-16-2 | 16 | PARTIAL [R-16.4]: TIME_ZONE/language hardcoded constants, currency implicit — make regional values env-driven Django settings (TIME_ZONE, currency, language) + .env.example documentation; conventions boundary: never-changes-at-runtime → env config, not SiteSettings | 4089–4238 [R-16.4] | PENDING (P3) | 0 |
| SPEC-16-3 | 16 | PARTIAL [R-15.1.1 hand-off]: semantic Success/Error color tokens in frontend/src/app/globals.css @theme (S15 audit assigned these to S16; errors currently reuse bronze — passes contrast, consistency polish) | 4089–4238 + S15[15.1.1] | PENDING (P3; smallest standalone win) | 0 |
| SPEC-16-4 | 16 | MISSING [R-16.15]: maintenance availability toggle on SiteSettings + storefront availability gating, cross-wired to SPEC-3-06's /maintenance page | 4089–4238 [R-16.15] | PENDING (P3; page itself → SPEC-3-06) | 0 |

Owner attributions (no duplicate tasks):
- R-16.1 literal /admin/settings route → **SPEC-5-09** (Django admin precedent; substance at /admin/ops/sitesettings/). R-16.5 tax display → **SPEC-3-12** + **SPEC-5-09**. R-16.6 guest checkout → **SPEC-3-02**. R-16.7 gateway config → **SPEC-5-09/5-11/3-13**. R-16.8 shipping settings → **SPEC-3-12/SPEC-1-07**. R-16.9 reservation TTL → **SPEC-12-01..03**. R-16.10 customer cancellation → **SPEC-3-18**. R-16.11 templates/SMS → **SPEC-3-14/S19**. R-16.12 SEO admin → **SPEC-5-07**. R-16.13 security settings/MFA → **S17**. R-16.15 page → **SPEC-3-06**. R-16.20 staging topology → **S22**.
- **P2 → owner S17**: V-02 fail-open DEBUG default (config/settings.py:34, pinned by tests/test_settings_security.py:39-50 with flip instruction) — flip to fail-closed + invert the pin when S17 opens.

Verified rules [R-16.16..16.22]: validation enforced + test-pinned; secrets env-only with CI scans + no-secrets log pin; sensitive-change audit via LogEntry + log_api_action; dev/prod env separation complete (12-factor); destructive-change confirmation mechanism exists and is applied; R-16.22 N-A vacuous-verified (no setting feeds order/payment logic today — becomes binding when tax/shipping/payment config lands; carried in owner rows).

Watch item: LOW_STOCK_THRESHOLD lives in env (ops-internal knob, consistent with conventions boundary) — must migrate to SiteSettings if it ever becomes merchant-facing (SPEC-3-04 PDP badge). Convention note: api_settings is a plain JsonResponse function view (grandfathered style, align-when-touch).
