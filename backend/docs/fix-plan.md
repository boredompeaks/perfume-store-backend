# Phase-by-phase fix plan

Ordered so that each phase is shippable and doesn't break the last. "Why" is stated for every item; finding IDs reference `audit.md` / `vulnerabilities.md`.

## Phase 0 — Repo & environment hygiene (done in audit session)

- [x] git repo + `.gitignore` (secrets/db/media excluded) — **why:** V-01 containment, nothing else matters if `.env` gets committed next week
- [x] pinned `requirements.txt` — **why:** F-03, reproducible builds and auditable dep versions
- [x] `.env.example` — **why:** documents required env without leaking values
- [x] docs suite + flagged findings — **why:** untracked findings get lost
- [ ] **Rotate Razorpay test keys** (manual, owner) — **why:** V-01, keys confirmed live during audit
- [ ] CI (GitHub Actions: check + makemigrations --check + test) — **why:** prevents regression on every push; zero-cost

## Phase 1 — Failure-proof the payment path *(highest business risk)*

1. **Razorpay webhook endpoint** (`/api/orders/webhook/`) with signature verification from `RAZORPAY_WEBHOOK_SECRET`; idempotent handler that confirms/refunds orders.
   **Why:** V-03/F-04 — today payment truth depends on the client calling back; a closed tab = paid order stuck in `pending` forever.
2. **Server-side payment reconciliation**: after signature verify, fetch the payment from Razorpay API and assert `amount == order.total_amount * 100` and `status == captured`.
   **Why:** the HMAC proves the payload came from Razorpay, not that the *amount* matches the order — price-drift/ID-confusion edge cases charge the wrong total.
3. **Stock-failure path → refund, not 409**: if stock/coupon fails at verify, trigger Razorpay refund API (or mark order `refund_pending` for a worker) and cancel the order.
   **Why:** V-03 — currently the customer is charged with no recourse.
4. **Order expiry**: management command or cron to cancel stale `pending` orders (and free nothing — stock isn't reserved) after N hours.
   **Why:** F-26 — orphaned orders and dangling Razorpay orders accumulate silently.
5. Wrap all Razorpay client calls: catch `razorpay.errors.RazorpayError`/`requests` exceptions → 503 with logged context.
   **Why:** V-07 — today a network blip is a raw 500.

## Phase 2 — Security hardening *( cheapest to do, biggest risk reduction )*

1. **DRF throttles**: global `UserRateThrottle`/`AnonRateThrottle` + scoped throttles: login/refresh (5/min), register (3/hr), password-reset + resend + verify (3/hr), apply-coupon (10/min anon), username-available (30/min).
   **Why:** V-04/V-11 — every public mutating endpoint is currently brute-forceable; coupon guessing is free money.
2. **Flip DEBUG default to `false`** + full prod settings block: `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` (start 3600), `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE='Lax'`, `SECURE_PROXY_SSL_HEADER`.
   **Why:** V-02/V-06 — a forgotten env var currently leaks stack traces; all five `check --deploy` warnings close at once.
3. **Enforce `validate_password` in `RegisterSerializer`** (same policy as reset).
   **why:** V-05 — `Password123` passes registration today.
4. **DB-level uniqueness for email**: migration to normalize + unique constraint (or custom user model), catch `IntegrityError` → 400.
   **Why:** V-08 — duplicate accounts possible under concurrency; also blocks future email-based flows from ambiguity.
5. Stop returning `session_id` in `CartSerializer`.
   **Why:** V-09 — needless token exposure; the client already owns the cookie.
6. Narrow CORS: explicit `CORS_ALLOW_METHODS`/`CORS_ALLOW_HEADERS` lists; reconsider `CORS_ALLOW_CREDENTIALS` if cart moves to JWT.
   **Why:** V-12 — least-privilege for credentialed CORS.
7. Admin hardening: move URL, document 2FA/strong-password policy.
   **Why:** V-18 — default `/admin/` is the top bot target.

## Phase 3 — Correctness of money & inventory logic

1. **Extract coupon service module** (`orders/services.py`: `validate_coupon_for_cart`, `compute_discount`) used by `create_order` AND `apply_coupon`; add Decimal `quantize(0.01)` on all discount math.
   **Why:** F-21 (duplication already drifting) + F-11 (runtime-verified: preview said 599.997, checkout stored 600.00 — customers see one total and get charged another).
2. **Stock re-validation at checkout**: before creating the order, check availability (cheap SELECT); fail fast with 409 and a clear error instead of post-payment.
   **Why:** V-10/F-13 — user currently discovers unavailability after paying.
3. **Per-user coupon limit** (new model field or usage table).
   **Why:** F-14 — a single user can drain a global coupon.
4. **Default ordering**: `ordering = ['-created_at', 'id']` on Product Meta (or default `.order_by()` in the view).
   **Why:** F-12 — runtime `UnorderedObjectListWarning`; pagination can repeat/skip rows.
5. **Slug race fix**: try/except `IntegrityError` retry loop (or rely on unique constraint + retry), page size from settings.
   **Why:** V-15 + F-23 (page size 2 is clearly a leaked dev value).
6. Rename `products` model → `Product` with `db_table`/migration.
   **Why:** F-22 — convention; do it after code stabilization to avoid churn.

## Phase 4 — Architecture & API quality

1. **Cart ownership**: attach cart to `user` when authenticated (nullable FK), merge session cart on login.
   **Why:** F-18 — logout/new device loses carts; checkout depends on cookie+jwt combo that's fragile.
2. **Replace FBVs with DRF CBVs/ViewSets + permission classes**; delete 4 duplicated `is_staff` blocks.
   **Why:** F-20 — consistency and fewer missed checks on future endpoints.
3. Serializer field whitelists (`ProductSerializer`, drop `user` from `OrderSerializer` response).
   **Why:** V-19.
4. Ops baseline: `LOGGING` config (request errors + payment failures), `/health/` endpoint, `/api/v1/` version prefix, gunicorn+nginx/Docker notes in README, media strategy (S3 or whitenoise for prod).
   **Why:** F-16/F-27 — currently nothing to monitor and media breaks at DEBUG=false.

## Phase 5 — Tests & CI gate *(starts in Phase 1, completes here)*

- Implement the 39 unit + 12 e2e tests enumerated in `test-gaps.md`, Razorpay always mocked.
- Coverage gate ≥ 80%; CI red on `makemigrations --check` drift.
- **Why:** F-05 — payment/coupon/stock logic is currently 100% untested; the Phase 1–3 refactors are unsafe without a net, so tests land alongside each phase and this phase closes the remaining backlog.

## Phase sequencing rationale

```
Phase 0  containment      (stop the bleeding)
Phase 1  money & trust    (customer-harm bugs first)
Phase 2  hardening        (cheap, orthogonal, no logic changes)
Phase 3  logic fixes      (needs tests from 2.5 onward to be safe)
Phase 4  architecture     (only after behavior is correct)
Phase 5  full test gate   (consolidates everything)
```

Razorpay key rotation (manual) can happen anytime — do it first.
