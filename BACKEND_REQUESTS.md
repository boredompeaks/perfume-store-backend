# Backend Requests

Gaps in the backend that the frontend needs, logged instead of worked around silently.
Format per entry: priority, where needed, what's missing, why, suggested contract (proposal only), current workaround.

---

## [P0] Razorpay webhook + server-side reconciliation
- **Where it's needed:** checkout payment step (`/checkout` → payment verify)
- **What's missing:** `/api/orders/webhook/` listener and post-verify reconciliation/refund (V-03/F-04). Payment truth currently arrives only via the client callback.
- **Why:** a closed tab after capture leaves orders stuck in `pending` forever; a stock/coupon failure at verify returns 409 *after* money is captured with no refund path.
- **Suggested contract:** `POST /api/orders/webhook/` with `RAZORPAY_WEBHOOK_SECRET` signature check, idempotent handler confirming/refunding; refund-or-`refund_pending` on verify-time stock/coupon failure.
- **Current workaround:** frontend makes every verify failure explicit (400 → retry payment; 409 → "payment captured, contact support" dead end with no retry affordance — the UI structurally cannot re-enter payment/verify from a 409). The frontend also cannot *distinguish* a "captured-but-409'd" pending order from a "never paid" one — both look identical in `GET /api/orders/` — so it cannot safely vary the pending-order UI either. A distinguishing state would let the UI show the right message.

## [P0] Products page size (2) + stable default ordering
- **Where it's needed:** product listing page (`/products`)
- **What's missing:** realistic page size (hardcoded `2` at `products/views.py:89`, F-23) and a default ordering (F-12) so pages are stable.
- **Why:** a store PLP showing 2 products per page is unusable; unordered pagination can repeat/skip rows across requests.
- **Suggested contract:** page size ~12–24 via settings; `ordering = ['-created_at', 'id']` on Product Meta.
- **Current workaround:** page-number pagination UI built to the current envelope; grid layout degrades gracefully with 2 items.

## [P1] Categories endpoint
- **Where it's needed:** PLP filter bar, home page navigation
- **What's missing:** any way to enumerate product categories (`category` is a free-text CharField; no list endpoint).
- **Why:** category nav/filters can't be rendered from real data.
- **Suggested contract:** `GET /api/products/categories/` → `[{category, count}]` (distinct, ordered).
- **Current workaround:** PLP accepts `?category=` (works), but the filter list is empty/derived; home avoids category tiles rather than fabricating them.

## [P1] Cart-to-user merge / user-owned carts
- **Where it's needed:** login flow, checkout
- **What's missing:** cart attached to the user (nullable FK) and merged on login (F-18/V-14). Checkout requires the *same* session cookie that built the cart plus the JWT.
- **Why:** a new device or expired session silently loses the cart; checkout then 404s ("Cart not found"), which reads as a bug to customers.
- **Suggested contract:** `Cart.user` FK; on login merge session cart into user cart; cart endpoints accept JWT auth with cookie fallback.
- **Current workaround:** frontend detects checkout/apply-coupon 404 and shows "cart session expired — add items again"; no data recovery.

## [P1] SIMPLE_JWT configuration
- **Where it's needed:** auth client (all JWT endpoints)
- **What's missing:** any SIMPLE_JWT settings block — defaults give ~5-minute access tokens with no rotation/blacklist.
- **Why:** every order request can hit a mid-flow expiry; silent refresh everywhere is compensating complexity, and a non-rotating refresh token is a long-lived bearer credential.
- **Suggested contract:** `SIMPLE_JWT = {ACCESS_TOKEN_LIFETIME: 15min+, REFRESH_TOKEN_LIFETIME: 7d, ROTATE_REFRESH_TOKENS: True, BLACKLIST_AFTER_ROTATION: True}`.
- **Current workaround:** frontend implements single-flight silent refresh on 401 and logout-on-failure.

## [P1] CSRF enforcement for session-cart mutations
- **Where it's needed:** all cart endpoints (`/api/cart/`, `/api/cart/{id}/`), checkout, apply-coupon
- **What's missing:** `SessionAuthentication` (or equivalent CSRF check) for cookie-authenticated mutations. `@api_view` sets `csrf_exempt` and `REST_FRAMEWORK` lists only `JWTAuthentication`, so DRF's `enforce_csrf` never runs. **Confirmed live 2026-09-18:** `POST /api/cart/` with only the session cookie and no `X-CSRFToken` returned 201, `DELETE` returned 200 — the docs' note that "session-cart endpoints are covered" does not hold.
- **Why:** any cross-site request that can attach the session cookie could mutate a victim's cart. SameSite=Lax-by-default mitigates today, but that is browser-behavior trust, not server enforcement.
- **Suggested contract:** add `SessionAuthentication` to `DEFAULT_AUTHENTICATION_CLASSES` (cart/checkout views then enforce CSRF automatically), or keep JWT-only and set `SESSION_COOKIE_SAMESITE='Strict'` explicitly. Once a CSRF cookie is issued, the frontend will read it and send `X-CSRFToken` on cart mutations.
- **Current workaround:** frontend always sends `credentials: 'include'`; no CSRF token can be sent because the API-only backend never issues a CSRF cookie.

## [P1] Throttles on public mutating endpoints
- **Where it's needed:** login, register, apply-coupon, verify-email, password-reset (V-04/V-11)
- **What's missing:** server-side rate limits (e.g. scoped DRF throttles per fix-plan Phase 2).
- **Why:** unthrottled apply-coupon is brute-forceable; frontend politeness (explicit Apply button, no keystroke calls) is not security.
- **Suggested contract:** per fix-plan.md Phase 2.1 (login 5/min, register 3/hr, apply-coupon 10/min, etc.) with `429` + `Retry-After`.
- **Current workaround:** coupon calls only on explicit submit; generic 429-aware error surface once backend returns them.

## [P1] Totals in the cart payload
- **Where it's needed:** `/cart` summary
- **What's missing:** `line_total` per item and a cart-level `subtotal` in `CartSerializer`.
- **Why:** the cart page currently computes display totals client-side — an estimate, not server truth (pricing must stay server-owned).
- **Suggested contract:** `items[].line_total` + cart `subtotal` (Decimal, server-computed).
- **Current workaround:** client multiplies `price * quantity` for display only; checkout/apply-coupon responses remain the source of truth for money.

## [P1] Same-site deployment requirement for the session cookie
- **Where it's needed:** production deployment topology; backend README deployment docs
- **What's missing:** any documentation (or explicit setting) of the cookie/site constraint between the frontend origin and the API origin. Found live in phase-5 browser testing: with the frontend on `localhost:3000` and the API base set to `127.0.0.1:8000`, the session cart was **silently broken** — Chrome's Lax-by-default cookie handling does not attach the `sessionid` cookie to cross-site fetches, so every cart request created a new orphan session while CORS and JWT kept working.
- **Why:** `localhost:3000` and `127.0.0.1:8000` are different sites (same for `app.example.com` + `api.example.com` if the registrable domain differs). Same-site subdomains (`www.` + `api.` of one domain) or one origin behind a reverse proxy are required for the session cart to function at all.
- **Suggested contract:** document the constraint in the backend README deployment section; consider setting `SESSION_COOKIE_SAMESITE='Lax'` explicitly (making the relied-upon browser default an explicit, auditable setting).
- **Current workaround:** frontend `.env.local`/PLAN.md pin `localhost:8000` (same-site in dev); the constraint is documented in `frontend/PLAN.md` §1.

## [P2] Coupon preview vs checkout rounding drift (frontend impact of F-11)
- **Where it's needed:** `/cart` coupon preview vs `/checkout` order total
- **What's missing:** quantized discount math shared by `apply_coupon` and `create_order` (backend audit F-11: preview returned 599.997, checkout stored 600.00).
- **Why:** the frontend shows the apply-coupon preview on the cart page and the order total after checkout — with unquantized math these can disagree by a paisa, which reads as a frontend bug to customers.
- **Suggested contract:** `Decimal.quantize(0.01)` on all discount math in a shared service (fix-plan Phase 3.1).
- **Current workaround:** the frontend always displays the most recent server response as truth (preview on cart; `Order.total_amount` after checkout) and labels checkout as the confirmation point, so a drift shows as a small change between steps rather than a wrong charge.

## [P2] Site settings editable in the Django admin
- **Where it's needed:** store contact details (support email, phone, WhatsApp number/message, Instagram) shown by the frontend
- **What's missing:** a `SiteSettings` model (singleton) exposed in the admin + a small `GET /api/settings/` endpoint, so the store owner can edit contact details without a redeploy.
- **Why:** the frontend currently reads contact details from build-time env/config (`src/lib/site.ts` + `NEXT_PUBLIC_*`) — fine, but every change needs a rebuild and a commit.
- **Suggested contract:** `GET /api/settings/` (public) → `{support_email, support_phone, whatsapp_number, whatsapp_message, instagram_url}`; admin-edited singleton with cache-friendly semantics (short revalidate).
- **Current workaround:** env/config single-source in `src/lib/site.ts`; the storefront renders only configured channels and hides the rest.

## [P2] Single-order endpoint
- **Where it's needed:** order detail page (`/orders/[id]`)
- **What's missing:** `GET /api/orders/{id}/` — only the unpaginated list endpoint exists, so the detail page fetches the whole list and finds the order client-side.
- **Why:** works, but scales poorly and pulls every order to render one.
- **Suggested contract:** `GET /api/orders/{id}/` (JWT, owner-scoped) → `Order`.
- **Current workaround:** list + client-side find.

## [P1] Current-user endpoint + shipping persistence (checkout prefill)
- **Where it's needed:** header/account UI after login, and the checkout address form
- **What's missing:** `GET /api/accounts/me/` returning identity, and any server-side source for repeat customers' shipping details (`full_name`, `phone`, `address`, `city`, `state`, `pincode` are only ever stored on an `Order`).
- **Why:** identity display needs `{id, username, email}` (login returns only the JWT pair; the payload carries only `user_id`). More importantly, checkout fields start blank on every order — "re-type your address every purchase" is real repeat-customer friction, not a nice-to-have.
- **Suggested contract:** `GET /api/accounts/me/` (JWT) → `{id, username, email, last_shipping: {full_name, phone, address, city, state, pincode} | null}` — `last_shipping` can be sourced from the user's most recent `Order` (no new model required); a `Profile` model is the alternative.
- **Current workaround:** header shows a generic "Orders" entry point (no identity display); the checkout form will prefill from a `localStorage` copy of the last successfully-used address — same-browser only, not cross-device, and not server truth. Treated as PII: cleared on logout, visible "saved on this device" disclosure at the point of prefill, and an explicit "forget saved details" control (phase 5).

## [P2] Product gallery (multiple images)
- **Where it's needed:** PDP
- **What's missing:** more than one image per product (single `ImageField`).
- **Why:** photography-led PDP is impossible with one shot; also blocks any zoom/gallery UX.
- **Suggested contract:** `Product.images` reverse relation → `[{id, image, alt}]`.
- **Current workaround:** single image, no gallery gestures (deliberately absent from the frontend gesture inventory).

## [P2] Production media serving
- **Where it's needed:** all product imagery
- **What's missing:** media serving when `DEBUG=False` (`static()` helper is a no-op; F-16/V-13).
- **Why:** every product image 404s in a production deployment.
- **Suggested contract:** whitenoise/S3 media strategy per fix-plan Phase 4.4.
- **Current workaround:** placeholder component renders for broken images.

## [P2] Customer-facing order cancellation (pending only)
- **Where it's needed:** order detail page
- **What's missing:** endpoint to cancel a `pending` order (no payment yet), so users can abandon failed payment attempts cleanly.
- **Why:** pending orders accumulate (F-26) and the orders list shows un-actionable rows.
- **Suggested contract:** `POST /api/orders/{id}/cancel/` → allowed only for owner + `status=pending`; guarded against cancelling after payment.
- **Current workaround:** UI explains pending state and offers "Retry payment" only.

## [P2] Explicit availability flag on Product
- **Where it's needed:** PLP cards, PDP
- **What's missing:** an explicit `in_stock`/`available` boolean (frontend infers `stock > 0`; stock itself is a raw count).
- **Why:** avoids leaking stock levels and prevents the UI from implying inventory precision the backend doesn't manage (no reservations).
- **Suggested contract:** `in_stock: bool` in `ProductSerializer`.
- **Current workaround:** derives `stock > 0` and never displays numeric stock.
