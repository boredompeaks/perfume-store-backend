# Technical Audit — findings & runtime verification

Audit date: 2026-09-17 (static review + runtime smoke test, Python 3.14.2, Django 6.1.1).
Code was **not modified** during this audit; findings are tracked in `fix-plan.md`.

## Static findings

| # | Area | Finding | Ref | Severity |
|---|---|---|---|---|
| F-01 | Secrets | Razorpay KEY_ID/SECRET in committed-adjacent `.env`, no git repo protections; keys confirmed **valid live** by smoke test | `.env` | CRITICAL |
| F-02 | Config | `DJANGO_DEBUG` defaults to **true** (fails open) | `config/settings.py:32` | CRITICAL |
| F-03 | Build | No dependency manifest (now added: `requirements.txt`, pinned) | root | CRITICAL |
| F-04 | Payments | Money-captured-but-order-failed path: stock/coupon failure at verify returns 409 *after* Razorpay capture; no refund, no webhook, no reconciliation | `orders/views.py:568-589` | CRITICAL |
| F-05 | Tests | Zero tests (runtime-confirmed: `Found 0 test(s)`) | all `tests.py` | CRITICAL |
| F-06 | Security | No throttles/rate limits on any endpoint (login, register, coupon brute-force, token guessing) | `config/settings.py:150-154` | HIGH |
| F-07 | Security | Register bypasses `AUTH_PASSWORD_VALIDATORS` (`min_length=8` only) while reset uses full `validate_password` | `accounts/serializers.py:7-9` | HIGH |
| F-08 | Security | No production security settings: HSTS, SSL redirect, secure cookies, proxy header (5 `check --deploy` warnings W004/W008/W009/W012/W016) | `config/settings.py` | HIGH |
| F-09 | Payments | Razorpay exceptions other than SignatureVerificationError unhandled → raw 500s in create/verify | `orders/views.py:445-467,508-521` | HIGH |
| F-10 | Security | Registration race: iexact uniqueness checks are check-then-act → possible 500 `IntegrityError`; `User.email` not unique at DB level | `accounts/serializers.py:31-40` | MEDIUM |
| F-11 | Logic | Coupon discount not quantized: preview returned 599.997 but checkout stored 600.00 — preview and charged totals disagree | `orders/views.py:199-214` | MEDIUM |
| F-12 | Logic | Pagination without default ordering → `UnorderedObjectListWarning` (runtime-confirmed), inconsistent pages across requests | `products/views.py:89` | MEDIUM |
| F-13 | Logic | No stock re-check at checkout (only at cart-add and verify); oversell discovered post-payment | `orders/views.py:118-126` | MEDIUM |
| F-14 | Logic | Coupon has no per-user limit; one user can drain global usage | `orders/models.py:6-63` | MEDIUM |
| F-15 | Security | Session ID returned to client in cart response | `cart/serializers.py:32` | MEDIUM |
| F-16 | Ops | Media served via `static()` — no-op when DEBUG=False → images 404 in production | `config/urls.py:34-37` | MEDIUM |
| F-17 | Security | `CORS_ALLOW_CREDENTIALS=True` widens exposure if origins list loosens | `config/settings.py:162` | MEDIUM |
| F-18 | Architecture | Cart lives in anonymous session, orders in JWT identity; no merge on login; checkout fails without cookie | `orders/views.py:53-74` | MEDIUM |
| F-19 | Hygiene | Dead code block after return; missing blank line; trailing whitespace | `orders/views.py:280-282`, `cart/views.py:117`, `cart/serializers.py:38` | LOW |
| F-20 | Quality | Manual `is_staff` check duplicated 4×; should be permission classes | `products/views.py:113,172,198,225` | LOW |
| F-21 | Quality | ~100 lines duplicated coupon validation between `create_order` and `apply_coupon` (already diverging) | `orders/views.py:132-220` vs `285-407` | LOW |
| F-22 | Quality | Model named `products` (plural lowercase) violates convention | `products/models.py:5` | LOW |
| F-23 | Quality | Pagination page size hardcoded to `2` (dev value leaked) | `products/views.py:89` | LOW |
| F-24 | Quality | `ProductSerializer` uses `fields = '__all__'` | `products/serializers.py:7` | LOW |
| F-25 | Logic | Slug generation check-then-act race → possible 500 under concurrency | `products/models.py:30-41` | LOW |
| F-26 | Logic | Pending orders never expire; abandoned Razorpay orders accumulate | `orders/models.py` | LOW |
| F-27 | Ops | No LOGGING config, no health endpoint, no API versioning | `config/settings.py` | LOW |
| F-28 | Logic | Orphaned unverified accounts on SMTP failure (recoverable, but silent) | `accounts/views.py:53-59` | LOW |
| F-29 | Logic | Stray binary `products/1000378454.png` committed in app dir (untracked media) | `products/` | LOW |

## Runtime verification (e2e smoke, in-memory test DB, 20/20 passed)

Real `db.sqlite3` untouched; email captured by locmem backend; Razorpay live test call succeeded.

| Verified behavior | Result |
|---|---|
| Products list endpoint | PASS (200) |
| Register → 201, email queued | PASS |
| Login blocked pre-verification (401) | PASS |
| Email verification via outbox token | PASS |
| Login post-verification → JWT | PASS |
| Product create rejected for anonymous (403) | PASS |
| Product create by staff (201) + slug generation | PASS |
| Product detail by slug (200) | PASS |
| Cart get/create, add (201), patch qty (200) | PASS |
| Cart add over-stock rejected (400) | PASS |
| Coupon preview (iexact code match) | PASS |
| Checkout with coupon: server-side total, discount stored quantized | PASS |
| **Razorpay live order creation** (network + test keys valid) | PASS |
| Forged payment signature rejected (400) | PASS |
| Order list user-scoped | PASS |
| Username availability | PASS |
| Full password reset flow (request + confirm) | PASS |
| Product PATCH rejected for non-staff (403) | PASS |

New findings surfaced by runtime: F-11 (rounding drift), F-12 (pagination warning), F-01 confirmation (live keys work → rotation urgent).

## Framework checks

- `manage.py check` — 0 issues
- `makemigrations --check --dry-run` — no drift between models and migrations
- `manage.py test` — 0 tests
- `pip check` — no conflicts
- `check --deploy` (DEBUG=false) — 5 security warnings (see F-08)
