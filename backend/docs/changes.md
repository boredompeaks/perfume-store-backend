# Changelog

Running log of repo-level changes. Keep updated with every commit that matters. (Source-code behavior is unchanged as of this entry — audit only.)

## 2026-09-18 — Phase 5 test suite (39 unit + 12 e2e implemented, coverage gate live)

Per `docs/test-gaps.md` and `docs/fix-plan.md` Phase 5. Final state: **178 tests, all passing, 100.00% coverage** (`coverage report` gate at `fail_under = 90`).
- `common/testing.py` — shared test base (`ApiTestCase`): locmem email, dummy Razorpay keys (`rzp_test_TESTINGONLY…`), MD5 hasher, temp `MEDIA_ROOT` (test uploads never touch the real `media/`), JSON-capable DRF client, factories + a `razorpay_mock` helper. **No test can hit the network or the real keys from `.env`** (V-01 containment).
- `accounts/tests.py` (35), `products/tests.py` (35), `cart/tests.py` (18), `orders/tests.py` (45), `ops/tests.py` (17) — the enumerated unit tests plus security extras (cross-user token reuse, ownership scoping, ordering-whitelist injection probes).
- `tests/` package (28) — the 12 e2e scenarios (happy path, verification gate, reset-via-outbox, multi-user isolation, oversell & coupon races, product lifecycle snapshots, cart persistence, pagination stability, admin status machine) + V-02 settings subprocess tests + admin-surface smoke.
- Razorpay is always mocked (`unittest.mock.patch('orders.views.razorpay.Client')`); race tests resolve through the verify-time row locks deterministically.
- 8 flip tests marked `@expectedFailure` pin the *fixed* behaviour for open findings (V-05 password policy ×2, V-02 DEBUG fail-open, missing-email validation gap, F-11 rounding parity, F-12 default ordering, F-18 cart ownership, first-save slug overflow) — they turn green automatically when the fixes land.
- `.coveragerc` + `coverage==7.16.1` in requirements + `.github/workflows/backend-tests.yml` (check → makemigrations --check → coverage-gated test run).

Bugs found and fixed while writing the tests (all behaviour, no API changes):

- `accounts/views.py` `_get_user(None)` → 500 on missing `uid` (verify-email / password-reset confirm); now 400 per the documented contract.
- `cart/views.py` non-numeric `product_id` → 500 (`ValueError` uncaught); now 404 like an unknown product.
- `accounts/admin.py` double-registered `User` (suite could not even boot); added the standard `admin.site.unregister(User)` swap.
- `products/admin.py` `format_html` without args crashed the changelist whenever a product had stock 0 (`TypeError`); now `mark_safe`.
- `products/admin.py` `prepopulated_fields` on a readonly `slug` crashed every product change page (`KeyError`); removed (the model generates slugs).
- `templates/admin/adjust_stock.html` dropped the `_selected_action` selection on apply — the stock-adjustment action silently did nothing from the UI; selection is now preserved.

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

## 2026-09-18 — SPEC-1-01 (Section 1) — builder: permission_classes + explicit serializer fields

Per conventions.md:14/:18 (audit F-20, F-24; serializer half of V-19). Product write authorization moved from 4 duplicated inline `is_staff` checks to DRF `permission_classes`; `ProductSerializer` declares explicit `fields`. Public surface unchanged: same endpoints, same staff-only writes, same `403 {"detail": "Administrator access is required."}` body (canary `test_serializer_field_set_is_pinned` still green).
- `common/permissions.py` (new) — `IsAdminUserOrReadOnly`: SAFE_METHODS public, writes require `is_staff` (same gate as `IsAdminUser`). Raises `PermissionDenied` directly so guests keep the legacy 403 instead of DRF's 401 `NotAuthenticated` (JWT authenticator attached, no credentials). A blanket `IsAdminUser` was not usable: it would have locked anonymous catalogue GETs on the multi-method views.
- `products/views.py` — inline `request.user.is_staff` checks removed (was views.py:113/172/198/225); both FBVs decorated `@permission_classes([IsAdminUserOrReadOnly])`. Edge shift: anonymous writes to an unknown slug now answer 403 before the view body (was 404) — permission checks precede view code; no existence leak.
- `products/serializers.py` — `fields = '__all__'` → explicit whitelist of the exact 10 fields the API always exposed (`id, name, slug, description, price, size, stock, category, image, created_at`).
- `products/tests.py` — +8 tests: unit contract of `IsAdminUserOrReadOnly` (message body, SAFE_METHODS anon, staff-only writes via `assertRaises(PermissionDenied)`), API wiring (anonymous reads incl. OPTIONS stay 200, staff PATCH e2e, unknown-slug write → 403), serializer whitelist + drift guard against `products._meta.concrete_fields`.
- Suite: **186 passed** (8 `expectedFailure` flips unchanged), coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-1-02 (Section 1) — builder: throttle scopes on public mutating endpoints + uniform anonymous coupon errors

Per conventions.md "Every public mutating endpoint gets a throttle scope" / "Uniform responses on anonymous flows" (V-04, V-11). Public coupons/cart/auth mutations are now rate-limited and the coupon preview can no longer be used as a code-existence oracle. Coupon `apply_coupon` stays deliberately public (permission semantics unchanged).
- `config/settings.py` — `DEFAULT_THROTTLE_CLASSES = (ScopedRateThrottle,)`: views opt in per endpoint via `throttle_scope`; scope-less views are untouched. `DEFAULT_THROTTLE_RATES` env-driven with defaults: `coupon` 10/min, `cart` 60/min, `auth` 10/min (`THROTTLE_*_RATE` keys documented in `.env.example`).
- `orders/views.py` — `apply_coupon` decorated `@throttle_scope('coupon')`; **uniform anonymous message chosen: `{"error": "Invalid coupon code"}` (400)** for every failure reason (unknown / inactive / not-yet-valid / expired / usage limit / below minimum, replacing six distinct bodies incl. the `minimum_order_amount` detail — uniform for *everyone*, authenticated callers included, so the public preview never validates codes differentially). Cart resolution moved *before* coupon validation: a cartless caller now gets the same `404 {"error": "Cart not found"}` for every code instead of an existence oracle. Authenticated checkout keeps its differentiated messages (pinned contracts unchanged).
- `cart/views.py` — `cart_detail` (POST add) + `cart_item_detail` (PATCH/DELETE) scoped `cart` via new `CartMutationRateThrottle` (ScopedRateThrottle subclass that exempts safe methods, so GET never consumes the mutation budget).
- `accounts/views.py` / `accounts/urls.py` — `register` scoped `auth`; login moved to a thin `LoginView(TokenObtainPairView)` with `throttle_scope = 'auth'` (response contract = TokenObtainPairView's, unchanged). Token refresh left for a follow-up task.
- `common/testing.py` — `ApiTestCase._pre_setup` clears the default cache: DRF throttle history lives in the shared cache for the whole run, so per-test reset makes throttles deterministic regardless of configured rates.
- `orders/tests.py` (+3 net, incl. rewritten `test_coupon_rejections_are_uniform` pinning the exact uniform body), `cart/tests.py` (+3), `accounts/tests.py` (+3) — scope/rate wiring asserted on the view classes, engagement proven with tiny rates (`429` on coupon/cart-add/update, register, login), GET-exemption and rejection-consumes-budget proven. Engagement tests patch `ScopedRateThrottle.THROTTLE_RATES` (DRF binds rates at import, so `override_settings` cannot reach them) — no sleeps, no network.
- Suite: **196 passed** (8 `expectedFailure` flips unchanged), coverage **100.00%** (gate 90), `makemigrations --check` clean.

## Next (per fix-plan.md)

- Phase 0 remaining: rotate Razorpay keys, add CI.
- Phase 1: webhook + reconciliation + refund path.
