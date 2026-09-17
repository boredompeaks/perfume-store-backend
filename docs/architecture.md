# Architecture

## Overview

API-only Django 6.1 project (no server-rendered pages; only `/admin/` UI). Four apps plus project config:

```
config/      settings.py, urls.py (root router), wsgi/asgi
accounts/    identity: registration, JWT login, email verification, password reset
products/    catalog: Product model + CRUD API (staff-writable, public-readable)
cart/        Cart + CartItem keyed by Django session (anonymous users)
orders/      Order, OrderItem, Coupon; Razorpay payment create/verify
```

## Authentication model (hybrid)

| Concern | Mechanism |
|---|---|
| Orders | JWT (`rest_framework_simplejwt`), `IsAuthenticated` on order endpoints |
| Cart | Django session cookie (anonymous sessions), no auth required |
| Products read | public; writes gated by manual `request.user.is_staff` checks |
| Accounts flows | public, deliberately enumeration-safe |

Consequence: the cart lives in the anonymous session while orders attach to the JWT identity. Checkout (`orders/views.py:53-74`) requires the session key to locate the cart, so clients must send cookies *and* the Bearer token. There is no cart-to-user merge on login.

## Data flow — purchase path

```
1. POST /api/cart/            session cart, stock checked (no lock)
2. POST /api/orders/checkout/ server-side pricing, coupon validation,
                              Order + OrderItems snapshot created (status=pending)
3. POST /api/orders/payment/  Razorpay order created (idempotent via
                              order.razorpay_order_id), amount in paise
4. Client pays via Razorpay Checkout
5. POST /api/orders/payment/verify/
      HMAC signature verified  (razorpay.utility.verify_payment_signature)
      transaction.atomic + select_for_update on Order, Products, Coupon
      idempotency: reject if status != pending or razorpay_payment_id set
      stock decremented, coupon.used_count++, status -> confirmed,
      paid items removed from session cart
```

Inventory, coupon usage, and cart cleanup happen **only** after payment verification (deliberate design; comment at `orders/views.py:249-250`).

## Money handling

- `Decimal` everywhere; `OrderItem.price` snapshots the product price at checkout.
- Discount: percentage (with optional `maximum_discount` cap) or fixed; clamped to subtotal.
- `Order.total_amount` is computed server-side; the client never sends amounts.
- Razorpay amount = `total_amount * 100` (paise).

## Settings profile

- Env-driven via python-dotenv (`config/settings.py`).
- `SECRET_KEY` fallback guarded: startup fails if `DJANGO_DEBUG=false` and no key (line 36-37).
- SQLite (`db.sqlite3`); no alternate DB engine configured.
- CORS allow-list + `CORS_ALLOW_CREDENTIALS=True` (needed for the session cart).
- Email via configurable SMTP backend; verification/reset links point at `FRONTEND_URL`.

## Known architectural constraints

1. No webhook listener — payment truth arrives only via the client callback (step 5). If the client never calls back, the order stays `pending` with money possibly captured.
2. No stock reservation between checkout and payment; oversell is reconciled (409) at verify time, *after* money is captured.
3. No cart ownership — carts are unguessable only by session id entropy.
4. No API versioning prefix (`/api/`, not `/api/v1/`).
5. Media served by `django.conf.urls.static.static()` — no-op when `DEBUG=False`.
