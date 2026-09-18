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
- `orders/tests.py` (+4 net, incl. rewritten `test_coupon_rejections_are_uniform` pinning the exact uniform body), `cart/tests.py` (+3), `accounts/tests.py` (+3) — scope/rate wiring asserted on the view classes, engagement proven with tiny rates (`429` on coupon/cart-add/update, register, login), GET-exemption and rejection-consumes-budget proven. Engagement tests patch `ScopedRateThrottle.THROTTLE_RATES` (DRF binds rates at import, so `override_settings` cannot reach them) — no sleeps, no network.
- Suite: **196 passed** (8 `expectedFailure` flips unchanged), coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-1-03 (Section 1) — builder: registration enforces validate_password (same policy as reset)

Per conventions.md:19 ("Registration and password reset must run `validate_password` — the same policy everywhere"; V-05). `RegisterSerializer` dropped its bare `min_length=8` in favour of Django's shared password validators, so registration and reset can no longer disagree about password strength.
- `accounts/serializers.py` — `password` field lost the `min_length=8` crutch; new object-level `validate()` runs `validate_password(password, user=<transient User built from the submitted username/email>)`. The transient user is what makes `UserAttributeSimilarityValidator` compare against the username/email being registered — the same user-aware call the reset path (`accounts/views.py:175`) makes against the target account. MinimumLength (8), Common and Numeric validators all come from `AUTH_PASSWORD_VALIDATORS` (`config/settings.py`), so both flows enforce one policy with zero duplicated configuration.
- Error contract preserved (no frontend change): rejections raise `serializers.ValidationError({'password': list(error.messages)})` — the identical `{"password": ["message", ...]}` shape reset-confirm returns and `RegisterForm.tsx` renders via `fieldErrors.password.map(...)`. Uniform anonymous flow: no new response shapes, no new enumeration surface (a policy failure reveals nothing about username/email existence).
- `accounts/tests.py` (+2 net — 4 tests now assert the fixed policy: the 2 un-marked V-05 flips + 2 new): the two V-05 `@expectedFailure` flip tests (common password, numeric-only) were un-marked per their own docstrings and now assert the fixed behaviour live, with added message-content assertions; new `test_password_similar_to_submitted_attributes_rejected` (username-similar and email-equal passwords rejected — serializer-level user-awareness) and `test_policy_errors_match_reset_path_shape_and_block_creation` (list-of-strings shape parity with reset-confirm + a rejected password creates no user row and sends no verification email). The remaining `expectedFailure` (missing-email gap) is untouched — that is a different finding.
- Suite: **198 tests, OK (192 pass, 6 `expectedFailure` — the two V-05 flips turned green)**, coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-1-22 (Section 1 follow-up) — builder: throttle remaining public mutating endpoints + the username-available existence oracle

Per conventions.md "Every public mutating endpoint gets a throttle scope" (V-04 follow-up to SPEC-1-02). The six remaining public account routes are now rate-limited, and the public GET username-existence oracle loses its unbounded probe budget. Anonymous response contracts unchanged: the uniform 200 bodies on the recovery flows are pinned by a dedicated test — throttling only adds a 429 refusal.
- Scope choice (documented): `resend-verification`, `forgot-username`, `password-reset` — the three email-sending flows — get a dedicated, tighter `recovery` scope (default **5/min**, env `THROTTLE_RECOVERY_RATE`): each accepted request triggers an outbound email, so the budget *is* the mail-bomb bound. The non-mail mutations `verify-email` and `password-reset/confirm` join the existing `auth` budget (10/min) alongside register/login/token-refresh; their one-time tokens bound replay further. `username-available` (GET) and `token/refresh` are scoped `auth` too — `ScopedRateThrottle` has no safe-method exemption, so the GET oracle is throttled like the rest.
- `accounts/views.py` — `@throttle_scope` added to the six FBVs (`username_available`, `verify_email`, `reset_password` → `auth`; `resend_verification`, `forgot_username`, `request_password_reset` → `recovery`). No view-body changes: uniform anonymous bodies (no existence leaks) byte-identical.
- `accounts/urls.py` — token refresh routed through a thin `RefreshView(TokenRefreshView)` with `throttle_scope = 'auth'` (response contract = SimpleJWT's, unchanged).
- `config/settings.py` / `.env.example` — new env-driven `THROTTLE_RECOVERY_RATE` (default 5/min) documented alongside the other `THROTTLE_*_RATE` keys.
- `accounts/tests.py` (+9 net) — `test_all_account_urlpatterns_declare_throttle_scopes` walks the real URLConf asserting a scope on every route (a future endpoint cannot ship unthrottled silently); 7 engagement tests prove 429 with tiny rates via the established `mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", ...)` + explicit `cache.clear()` pattern (verify-email, reset-confirm, token-refresh via `RefreshToken.for_user`, the GET oracle, and the three recovery flows — each also asserts the throttled request triggers **no** further email); `test_recovery_throttling_preserves_uniform_bodies` pins the exact 200 bodies of the three recovery flows under a throttled config (known and unknown addresses alike).
- Suite: **207 tests, OK (201 pass, 6 `expectedFailure` flips unchanged)**, coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-5-01 (Section 5) — builder: dashboard aggregate orders, AOV and pending-fulfilment KPIs

Per spec §5.1 dashboard mockup (Orders / Average order value / Pending fulfilment cards). The dashboard previously rendered only per-status cards: no aggregate order count, no AOV anywhere, and its "pending" metric was payment-pending rather than true pending-fulfilment.
- `ops/services.py` — `get_stats()` now also returns `average_order_value` (Decimal end-to-end: paid revenue ÷ paid order count, quantized to `0.01` before serializing; guarded on the paid **count**, so an empty or all-unpaid store yields `0.00` instead of ZeroDivisionError) and `orders_pending_fulfilment` (count of status `confirmed`). "Paid" is exactly the existing gross-sales set `REVENUE_STATUSES = ("confirmed", "shipped", "delivered")` — `verify_payment` flips pending→confirmed at capture — and one aggregate query now yields revenue + paid count together. Pending fulfilment is deliberately only `confirmed` (paid, awaiting shipment): `shipped`/`delivered` have left the warehouse; `pending` is payment-pending. `/health/` untouched.
- `ops/templates/ops/dashboard.html` — three KPI cards added in the existing grid pattern: aggregate **Orders** (all statuses), **Average order value** (₹, subnote "paid revenue ÷ paid orders"), **Pending fulfilment** (subnote "paid, awaiting shipment (confirmed)"). The per-status cards (incl. `Orders · pending`) remain, with an explicit "awaiting payment" subnote so the payment-pending meaning is not silently repurposed. The admin-index `dashboard_cards.html` widget is untouched (additive stats keys keep it working).
- `ops/tests.py` — +4 net tests: AOV correctness on seeded mixed statuses (265.75/3 → "88.58", unpaid + cancelled orders excluded from both sides), zero-paid-orders guard (empty store and unpaid-only store → "0.00", no crash), pending-fulfilment counting only `confirmed` while `pending` stays a separate metric, and a staff dashboard render test asserting all three cards with real numbers plus the retained payment-pending labeling.
- Suite: **211 tests, OK (205 pass, 6 `expectedFailure` flips unchanged)**, coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-5-02 (Section 5) — builder: env-driven low-stock threshold + dashboard N+1 fix

Ops convention conformance (conventions.md env hygiene): the low-stock threshold was a hardcoded module constant and the dashboard resolved each recent-order customer with its own `User.objects.get` (N+1).
- `config/settings.py` — new `LOW_STOCK_THRESHOLD` setting read from the environment with default 5; parsed via a small `_env_int` helper so a malformed env value falls back to the default instead of crashing startup (a bare `int(os.getenv(...))` would 500 the whole app on bad input).
- `ops/services.py` — the hardcoded `LOW_STOCK_THRESHOLD = 5` constant is gone; `get_health()` reads `settings.LOW_STOCK_THRESHOLD` at call time via `_low_stock_threshold()` (not import time, so env changes and test overrides take effect immediately); the payload still echoes the configured threshold, degraded-DB path included.
- `ops/views.py` — the dashboard's per-order `User.objects.get(pk=row["user_id"])` loop is replaced by one batched `User.objects.in_bulk(user_ids)`; a user row that vanishes between `get_stats()` and the fetch degrades to the same "—" placeholder instead of crashing; rendered context keys and values are unchanged.
- `.env.example` — documents `LOW_STOCK_THRESHOLD=5` with its meaning and bad-value behavior.
- `ops/tests.py` — +6 net tests: threshold override reclassifies products on the next `get_health()` call with the echoed value updated; `_env_int` parses valid values and falls back on garbage; the dashboard low-stock table and its "(≤ n)" heading follow the override; recent-orders context shape pinned exactly (stats keys + one resolved username); N+1 regression guard (3 distinct customers ⇒ exactly one batched `auth_user … IN` query); a user vanishing mid-render yields the dash placeholder, not a 500.
- Suite: **217 tests, OK (211 pass, 6 `expectedFailure` flips unchanged)**, coverage **100.00%** (gate 90), `makemigrations --check` clean.

## 2026-09-18 — SPEC-5-03 (Section 5) — builder: sales-over-time chart on admin dashboard from real order data

Per spec §5.1 ("Illustrative chart only — actual dashboard must use real order and payment data"). The dashboard had no time-series view at all; this also fulfils the deferred SPEC-1-10 time-series analytics. Server-side render only: no JS, no CDN, admin works offline.
- `ops/services.py` — new `get_sales_series(days=None)`: daily revenue + order counts over a trailing calendar window ending today, over **paid orders only** — the same `REVENUE_STATUSES` set as the gross-sales KPI, so chart and revenue card can never disagree. Query shape: **one** grouped query (`TruncDate("created_at")` → `values("day")` → `Sum("total_amount")` + `Count("id")`, date-range filtered), then Python zero-fills the calendar range so the series has no gaps; ordered oldest → newest as `{date, revenue, orders}` dicts. Decimal end to end, quantized to `0.01` before leaving the function.
- `config/settings.py` + `.env.example` — `DASHBOARD_SALES_WINDOW_DAYS` (default 30) parsed via the existing `_env_int` (malformed values fall back to the default), following the `LOW_STOCK_THRESHOLD` precedent; `get_sales_series` reads it at call time and clamps nonsensical (≤0) values to a single day. An explicit `days=` argument overrides the setting.
- `ops/views.py` — dashboard context gains `sales_series`, `sales_max_revenue` (bar-height scale) and `sales_summary` (one-line accessible name stating the real paid-order count and revenue total); all computed from the same series.
- `ops/templates/ops/dashboard.html` — "Sales over time — Last N days" module between the KPI cards and the health monitor: a CSS bar chart (plain flex divs, height = `{% widthratio day.revenue sales_max_revenue 100 %}%`, grey 3px stubs for zero days, per-bar `title` tooltips), wrapped in `<figure role="img" aria-label="{{ sales_summary }}">` for screen readers, plus a collapsible "View as data table" with every day's exact date/revenue/orders as the non-visual alternative.
- `ops/tests.py` — +9 net tests (`SalesSeriesTests` ×6: gapless default window ordered oldest→newest ending today; per-day revenue/count sums from real paid orders with pending/cancelled/out-of-window money appearing on no day; `days=3` boundary — day-2 in, day-3 out; zero-fill between busy days; env-tunable window with explicit-arg override; ≤0 window clamps to one day. `DashboardSalesChartTests` ×3: chart renders the "Last 30 days" label with real values from the series context while unpaid money stays out; `aria-label` states the real totals ("2 paid orders, total revenue ₹72.00"); the env window drives label and series length together). Staff gating unchanged (no new URL; existing anonymous-redirect test still passes).
- Suite: **226 tests, OK (220 pass, 6 `expectedFailure` flips unchanged)**, coverage **100.00%** (gate 90), `makemigrations --check` clean.

## Next (per fix-plan.md)

- Phase 0 remaining: rotate Razorpay keys, add CI.
- Phase 1: webhook + reconciliation + refund path.
