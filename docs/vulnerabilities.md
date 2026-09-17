# Flagged vulnerabilities & security weaknesses

Severity counts: **3 CRITICAL · 4 HIGH · 8 MEDIUM · 6 LOW** (21 total).

| ID | Severity | Vulnerability | Location | Status |
|---|---|---|---|---|
| V-01 | CRITICAL | Live payment credentials in `.env` (confirmed working by live API call) — rotation required | `.env` | OPEN |
| V-02 | CRITICAL | DEBUG fails open — missing env var enables debug mode in prod | `config/settings.py:32` | OPEN |
| V-03 | CRITICAL | No refund/webhook reconciliation: customer charged, order can remain unfulfilled (409 path) | `orders/views.py:568-589` | OPEN |
| V-04 | HIGH | No rate limiting: credential stuffing (login), coupon brute-force (unauthenticated `apply-coupon`), token guessing on verify/reset, registration spam | DRF settings + all public POST views | OPEN |
| V-05 | HIGH | Weak registration password policy — only `min_length=8`, validators bypassed (`Password123` passes) | `accounts/serializers.py:7-9` | OPEN |
| V-06 | HIGH | No HTTPS/HSTS/secure-cookie/proxy-SSL settings (5 deploy warnings W004/W008/W009/W012/W016) | `config/settings.py` | OPEN |
| V-07 | HIGH | Unhandled Razorpay/network exceptions → 500s leak stack traces when DEBUG=true | `orders/views.py:445-467,508-521` | OPEN |
| V-08 | MEDIUM | Registration race conditions: duplicate email/username possible under concurrency → 500s | `accounts/serializers.py:31-40` | OPEN |
| V-09 | MEDIUM | Session ID exposed to client | `cart/serializers.py:32` | OPEN |
| V-10 | MEDIUM | No stock check at checkout — price/availability drift between cart-add and payment | `orders/views.py:118-126` | OPEN |
| V-11 | MEDIUM | Coupon brute-force aided by unthrottled unauthenticated preview endpoint | `orders/views.py:284+` | OPEN |
| V-12 | MEDIUM | `CORS_ALLOW_CREDENTIALS=True` (requires allow-list discipline; no `CORS_ALLOW_METHODS`/headers narrowing) | `config/settings.py:162` | OPEN |
| V-13 | MEDIUM | Media files served by dev static helper; production serving path undefined | `config/urls.py:34-37` | OPEN |
| V-14 | MEDIUM | Cart ownership by raw session id; no merge/rebinding on login; stale carts accumulate | `cart/models.py` | OPEN |
| V-15 | LOW | Slug generation race → 500 IntegrityError under concurrent creates | `products/models.py:30-41` | OPEN |
| V-16 | LOW | `username-available` enables username enumeration | `accounts/views.py:79-93` | OPEN |
| V-17 | LOW | Unverified-but-active users: verification tokens remain valid after activation (account takeover vector only if email compromised; acceptable, note only) | `accounts/views.py:96-106` | OPEN |
| V-18 | LOW | Admin at default URL, no 2FA/lockout | `config/urls.py:10-11` | OPEN |
| V-19 | LOW | `ProductSerializer` `__all__` — future column leaks; `OrderSerializer` exposes `user` id | serializers | OPEN |
| V-20 | LOW | Pagination unordered (data-integrity of listings, also DoS-light via repeated full scans) | `products/views.py:89` | OPEN |
| V-21 | LOW | No security headers beyond Django defaults (XSS protection config removed in modern Django; X_FRAME_OPTIONS default) | `config/settings.py` | OPEN |

## Notes

- No SQL-injection surface: ORM used throughout; `ordering` whitelisted.
- No XSS surface server-side (JSON API), but response errors echo `serializer.errors` — safe by default in DRF.
- CSRF middleware active; JWT flows are header-based (no CSRF needed); session-cart endpoints are the CSRF-sensitive surface and are covered.
- Password reset / username lookup flows are enumeration-safe (uniform responses).
- Payment signature verification is correctly implemented and was runtime-verified against a forged signature.
