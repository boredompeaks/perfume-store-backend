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

Consequence: the cart lives in the anonymous session while orders attach to the JWT identity. Checkout (`orders.views.create_order`) requires the session key to locate the cart, so clients must send cookies *and* the Bearer token. There is no cart-to-user merge on login.

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

Inventory, coupon usage, and cart cleanup happen **only** after payment verification (deliberate design; all three are inside `orders.views.verify_payment`, after its locked sufficiency re-check).

## Money handling

- `Decimal` everywhere; `OrderItem.price` snapshots the product price at checkout.
- Discount: percentage (with optional `maximum_discount` cap) or fixed; clamped to subtotal.
- `Order.total_amount` is computed server-side; the client never sends amounts.
- Razorpay amount = `total_amount * 100` (paise).

## Settings profile

- Env-driven via python-dotenv (`config/settings.py`).
- `SECRET_KEY` fallback guarded: startup fails if `DJANGO_DEBUG=false` and no key (the `raise RuntimeError` next to the `DEBUG` resolution in `config/settings.py`).
- SQLite (`db.sqlite3`); no alternate DB engine configured.
- CORS allow-list + `CORS_ALLOW_CREDENTIALS=True` (needed for the session cart).
- Email via configurable SMTP backend; verification/reset links point at `FRONTEND_URL`.

## Known architectural constraints

1. Payment truth is server-side too, not only client-supplied. `POST /api/v1/webhooks/razorpay/` (SPEC-1-06) verifies Razorpay's signature over the raw body, records every delivery in `PaymentEvent` (unique `event_id` = the replay defence), and reconciles a capture onto the order idempotently through the `orders/state.py` machine — so a dropped client callback no longer strands a paid order as `pending`. The client callback (`/api/orders/payment/verify/`) is still the surface that converts stock and clears the cart; an unconfigured webhook secret fails the endpoint closed (503). A refund event is recorded, never re-issued: the admin refund seam is the only writer of `Refund`.
2. Stock *is* reserved between checkout and payment (`StockReservation`: minted at checkout, converted on confirmation, released on a failed verify or a cancel, expired by the TTL reconciler), but the checkout availability gate is advisory — it compares against on-hand `stock`, not against available-to-sell net of other holds — so the authoritative anti-oversell check is still `verify_payment`'s locked re-check, which answers 409 *after* money is captured.
3. No cart ownership — carts are unguessable only by session id entropy (`Cart` has no user FK).
4. Two URL families: `/api/...` (legacy) and `/api/v1/{store,account,admin,webhooks}/...`, which reuse the same view objects. Legacy paths stay alive as aliases until the frontend base-URL cutover.
5. Media is served by an explicit `django.views.static.serve` route against `MEDIA_ROOT` (env-driven), deliberately last in the urlconf so an application route can never be shadowed by a media path. The old `static()` helper is gone because it returns an empty list when `DEBUG=False` — an upload would 404 in production with nothing logged.
