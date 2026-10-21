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

## [P2] Dispatch / delivery window fields on settings
- **Where it's needed:** `/shipping` dispatch-and-delivery copy (`src/lib/policies.ts`, `shippingPolicy`)
- **What's missing:** `dispatch_window_days` / `delivery_window_days` on `GET /api/settings/` (the endpoint answers with contact channels plus `return_window_days` only — spec A-12.G11).
- **Why:** shipping windows are owner-editable policy claims; today editing one is a code deploy, while the return window is already API-owned through the same endpoint.
- **Suggested contract:** add both as positive integers to the existing public settings reader — same admin singleton and 60s revalidate semantics as `return_window_days`.
- **Current workaround:** client-side constants `shippingPolicy` in `src/lib/policies.ts` (single source, labeled, comment names the future fields); consumers import them and `/shipping` renders them verbatim.

## [P1] Guest checkout mint path + guest token disclosure (spec CHANGE-11 / §12-G1)
- **Where it's needed:** `/checkout` guest path, guest order view (`/orders/[id]`)
- **What's missing:** an anonymous order-creation path that mints a `guest_token`. Verified 2026-10-15 against `backend/orders/views/checkout.py`: `create_order` is `@permission_classes([AllowAny])`, but its first body line refuses EVERY anonymous submission with one uniform 403 (`_guest_checkout_refused` — TIER1-GUEST-DEADEND) **before anything is read or written**: no request field produces a `guest_token`, the refusal mints no token, no order, no reservation. `OrderSerializer` never carries `guest_token`, so no response of the current contract can disclose one. `guest_order_detail` (GET `/api/orders/guest/`[<order_number>/], `X-Guest-Order-Token` header, uniform 404 on any miss) answers only pre-refusal legacy rows. Payment is separately account-scoped: `create_payment`/`verify_payment` are `IsAuthenticated`. Note: the refusal is a recorded owner decision (`backend/docs/run/TIER1-launch.md`, SPEC-3-02 off the Tier-1 list) — logged because the frontend spec (CHANGE-11, §12-G1, A-9 e2e row 1) still specifies the opposite flow and the storefront cannot reconcile the two itself.
- **Why:** spec §5.4 CHANGE-11 — the redesign's headline checkout change ("guest adds to cart → `/checkout` without signing in → address → create order → `guest_token` returned → pay handoff") cannot ship at all; the frontend can only surface the server's refusal as a sign-in hand-off.
- **Suggested contract:** restore the pre-refusal shape — anonymous `POST /api/orders/checkout/` carrying `guest_email` → 201 with `guest_token` disclosed exactly ONCE (the minting response only, never on dedup/idempotency collapses — the SPEC-1-B04 fix cycles' rule), token presented for reads via `X-Guest-Order-Token` on `GET /api/orders/guest/`; plus a token- or checkout-session-authorized payment path so a guest order can reach `confirmed` (the carried SPEC-1-B04 gap: "guest PAYMENT is not token-authorized").
- **Current workaround:** `featureFlags.guestCheckout` (default true) drops the checkout sign-in wall; a guest reaches review and, on the backend's uniform 403, sees the server's own refusal message with a sign-in hand-off — no order, no token, nothing minted client-side, no auth faked. The frontend plumbing is ready for a minting response: `src/lib/guest-token.ts` (sessionStorage, tab-scoped, cleared on explicit logout only) plus `fetchGuestOrder` send the header, and `checkout()` stores `guest_token` if a response ever carries it. `OrderDetailView` has no guest fallback yet (follow-up once minting exists).
## [P1] Restock opt-in is auth-only and ignores the email the storefront collects
- **Where it's needed:** sold-out PDP restock form (`src/components/RestockForm.tsx`, wired on `/products/[slug]`, U-W4-F3).
- **What's missing:** `POST /api/products/<slug>/restock-notifications/` (SPEC-19-4) is `IsAuthenticated` and never reads the request body, so (a) a signed-out visitor cannot register an address at all (401), and (b) the `email` the form collects is not the address the trigger mails — `_notify_back_in_stock` always sends to `user.email`, and `RestockNotification` has no address field.
- **Why:** the classic back-in-stock case is a guest leaving an address; today that path dead-ends at a sign-in prompt, and a signed-in customer typing a different address would silently be mailed at the account address instead — a mismatch the UI cannot detect or explain.
- **Suggested contract:** either (1) accept an optional validated `email` on the same endpoint for anonymous callers (the `restock` throttle already bounds abuse; store the address on the preference row), or (2) keep it auth-only but document that plus expose the signed-in address (e.g. `email` on a `/api/accounts/me/`, which does not exist yet — `tokens.ts` notes the JWT carries only `user_id`) so the form can confirm the real destination instead of collecting one it ignores.
- **Current workaround:** the form always POSTs `{email}` (ignored by today's view), renders every backend message verbatim (400/404/429/401), and on 401/403 shows the server message plus a "Sign in" link back to the PDP (`/login?next=/products/<slug>`).
- **[P2] In-tree vs promoted contract drift (BE-003/BE-008, folded by BE-010):** the in-tree backend still emits `CartSerializer.session_id` (`backend/cart/serializers.py` Meta.fields; its V-09 test is `@unittest.expectedFailure`) and register `user.id` (`backend/accounts/views.py` response, asserted at `backend/accounts/tests.py:122`), while the promoted feat/add-frontend @ a43f058 drops both (auditor-SHIPPED) — the frontend now follows the promoted contract (fields removed from `frontend/src/lib/types.ts` `Cart` and `RegisterResult.user`, U-W5-T2); next backend sync should bring the in-tree serializer/view to the promoted shape rather than the frontend re-adding the fields.

## [P2] Customer-facing copy for the register 503 (account created, verification email failed)
- **Where it's needed:** `/register` account-created state (`frontend/src/components/auth/RegisterForm.tsx`, U-W8-T1).
- **What's missing:** a customer-safe `error` string for the 503 branch of `POST /api/accounts/register/` (`backend/accounts/views.py`). The current string is operator-facing: "Account created, but verification email could not be sent. Check SMTP settings."
- **Why:** the storefront renders server copy verbatim by contract (UI-UX-SPEC §5.9, P5 — no client paraphrase of server text), so a customer is told to check SMTP settings, an instruction only an operator can act on.
- **Suggested contract:** same status (503) and `{error: ...}` shape, customer wording, e.g. "Your account was created, but the verification email could not be sent. You can request it again." (proposal only; the frontend branches on status, not on the message text, so this is copy-only).
- **Current workaround (if any):** the message renders verbatim in a `role="status"` panel with a `/resend-verification` link; no alternative copy is invented client-side.
## [P2] Contact / enquiry submission endpoint (spec §5.8, coverage contract §3.9)
- **Where it's needed:** `/contact` (`src/app/contact/page.tsx`)
- **What's missing:** a customer-facing contact-submission endpoint. Verified read-only 2026-10-16 against `backend/config/urls.py` (every `path()` / `include()`), `accounts/urls.py`, `products/urls.py`, `cart/urls.py`, `orders/urls.py`, `shipping/urls.py` and the `common` settings view: no route accepts a message/enquiry body. `GET /api/settings/` publishes contact *channels* only (`whatsapp_number`, `support_phone`, `support_email`, `instagram_url`, `return_window_days`).
- **Why:** the coverage contract's §3.9 one-liner is "Contact form and support channels"; spec §5.8 therefore ships the real channels and records that a form would be a backend change. Without an endpoint there is nothing to POST to, and shipping a form that posts nowhere would collect a message and discard it — so none is rendered.
- **Suggested contract:** throttled `POST /api/contact/` accepting `{name?, email, subject?, message}` → `202` with one uniform acknowledgement body regardless of content (no reflection, no existence signal), stored or forwarded server-side, reusing the existing public-endpoint throttle pattern.
- **Current workaround:** none needed by the UI — `/contact` renders the configured channels (WhatsApp / phone / email / Instagram, each hidden when unconfigured) and the four §5.8 sections; no form, no disabled control, no invented target.

## [P2] Carry `next` across the email-verification hop so the register → checkout funnel completes (D2 / U-W10-T2)
- **Where it's needed:** the post-registration chain `/register?next=/checkout` → `/verify-email` → `/login` (owner directive D2: anonymous `/checkout` is replaced to `/register?next=/checkout`).
- **What's missing:** a way for `?next=` to survive the verification email. Verified read-only 2026-10-17 against `backend/accounts/views.py`: `register` (lines 56-91) returns `{message, user}` and mints no session; `serializers.py:27` creates the user with `is_active=False`, so JWT login is refused until verification; `_send_verification_email` (line 48) builds the link as `{FRONTEND_URL}/verify-email?uid=…&token=…` only. Nothing accepts or echoes a `next` target.
- **Why:** D2 funnels an unauthenticated visitor to `/register?next=/checkout` and asks the chain to land them back on checkout after registering. The param is honoured by the frontend (`/register` and `/login` both read it through `safeNext`; `GuestOnly` and `LoginForm` act on it) but the backend-generated verification URL drops it, so the customer resumes at a bare `/login` and reaches checkout again only through the cart.
- **Suggested contract:** accept an optional `next` on `POST /api/accounts/register/`, validate it as an internal path only (mirror of the frontend `safeNext` rule: must start with a single `/`, no scheme, no `//`, no `\`), echo it into the verification URL as `/verify-email?uid=…&token=…&next=…`, and (frontend follow-up, not this request) let `/verify-email` pass it to its "Go to sign in" link as `/login?next=…`. Auto-login at register is explicitly NOT the proposal — `is_active=False` until verification is a deliberate security posture.
- **Current workaround:** `/register` parses `?next=` server-side and passes it to `GuestOnly`, so an already-authenticated visitor is `router.replace`d straight to the target and the param stays in the URL for that page; the login door round-trips fully today (`/login?next=/checkout` → after sign-in → `/checkout`, source-pinned in `checkout.test.ts`). Across the email hop the param is dropped, and the storefront deliberately does not fake persistence for it (no sessionStorage carrying) — the customer continues at `/login`.

## [P2] Sample-vial request (the D1 carousel's secondary CTA has no endpoint)
- **Where it's needed:** home "New arrivals" carousel secondary action (`frontend/src/components/home/HomeCarousel.tsx`, U-W11-T4).
- **What's missing:** any sample-deck / sample-vial request endpoint. Verified read-only 2026-10-17 against the API surface the storefront actually binds (`frontend/src/lib/*-api.ts`: products, cart, orders, accounts, settings, tracking): nothing accepts a sample request, and `GET /api/settings/` publishes contact channels plus the return window only.
- **Why:** the D1 screen (`media/designs/maison_ther_catalogue_accueil_carousel_edition/code.html:269-272`) pairs every slide's primary flacon CTA with a "Request Sample Vial" link. There is nothing to submit it to, and a control that collects intent and delivers it nowhere would breach P5 (say the real thing), so the link is not rendered rather than rendered dead.
- **Suggested contract:** throttled `POST /api/samples/` accepting `{product_id, email}` returning one uniform `202` acknowledgement regardless of content (same no-oracle shape as the guest restock arm), or a per-product `sample_available` flag on the product serializer so the UI can gate the line on real availability.
- **Current workaround (if any):** each slide ships a single action, the `View flacon` link to the product detail page; the sample line is omitted entirely (not stubbed, not disabled) and the omission is cited in the U-W11-T4 report.

## [P2] Product lot code + concentration for the listing card meta line
- **Where it's needed:** `/products` listing card (`src/components/ProductCard.tsx`, U-W11-T5 screen parity)
- **What's missing:** per-product batch/lot code and concentration type (e.g. Extrait / Eau de Parfum). The screen design (`media/designs/maison_ther_all_fragrances_refined_cards_atmosphere/code.html`, card meta row) renders "50 ml Extrait … Lot 01" under each price; `ProductSerializer` exposes only `size` (ml).
- **Why:** the card meta line cannot reach the screen's anatomy without inventing data — P5 bans fabricated lot numbers, so the slot is left empty instead.
- **Suggested contract:** add `lot_code: string | null` and `concentration: string | null` (or a small enum) to `GET /api/products/` results.
- **Current workaround:** meta line renders `{size} ml` alone; the screen's lot/concentration slot stays empty until the fields exist.

## [P2] Structured olfactory notes on Product (top / heart / base)
- **Where it's needed:** `/products/[slug]` PDP — the olfactory pyramid section (U-W11-T6 slice 1; design screen `ther_s_culaire_extrait_de_parfum_refined_hero_flow`, "Olfactory Architecture").
- **What's missing:** any structured notes field. Read-only verified 2026-10-17 against `backend/products/models.py` and `serializers.py`: `GET /api/products/{slug}/` returns `id, name, slug, description, price, size, stock, category, image, created_at`, and `description` is one free-text string (spec §12-G10).
- **Why:** the design's Top/Heart/Base pyramid cannot be typeset honestly from a prose blob. The storefront ships an `OlfactoryPyramid` component but renders nothing until the server publishes tiers, because parsing or inventing notes from the prose would fabricate product claims (P5) and reintroduce the banned three-column grid in different clothes (§5.2 item 5).
- **Suggested contract:** add an optional object to the product detail payload: `"notes": { "top": [{"name": "…", "detail": "…"}], "heart": [...], "base": [...] }` — each level an array of objects, empty or absent levels simply omitted; `detail` is optional one-line provenance copy. List endpoints may omit the field entirely.
- **Current workaround (if any):** `toOlfactoryTiers(product.notes)` (frontend `lib/products-api.ts`) returns `[]` and the PDP renders no pyramid section; the free-text description keeps carrying the fragrance story as prose at measure (§5.2 item 5).

## [P2] Cart-level reservation banner state
- **Where it's needed:** `/cart` title band (U-W12-T7 screen parity; design `media/designs/maison_ther_votre_panier/code.html` renders a reservation banner - "Vos flacons sont temporairement reserves...", accents dropped here after a shell encoding mangling - plus a "Tirage restreint certifie" seal above the line items).
- **What's missing:** any cart-level hold signal. Read-only verified 2026-10-17: `GET /api/settings/` (backend/ops/views.py:111-134) publishes contact channels + `return_window_days` only, and `GET /api/cart/` (backend/cart/serializers.py) returns `id, items, coupon_code, created_at, updated_at`. Stock reservations are checkout-time (SPEC-12-01, config/settings.py reservation seconds) and are never surfaced to the cart.
- **Why:** the screen's reservation banner cannot be rendered honestly - the cart holds no stock and no expiry is published, so the claim would be invented (P5). The "certified limited run" seal has no backing field either.
- **Suggested contract:** either `reservation_expires_at: string | null` on the cart payload (if cart-level holds are ever introduced) or a public settings field (e.g. `cart_hold_policy: string | null`) the banner copy can quote verbatim.
- **Current workaround:** the banner is omitted from the cart page; the title band ships eyebrow + h1 + real unit count + static step rail only.

## [P2] E2E-reachable inventory pin (deterministic oversell for the 409 dead-end e2e)
- **Where it's needed:** `frontend/e2e/checkout.spec.ts` "409 captured-but-unfulfillable → dead end" payment leg (gated on `RAZORPAY_TEST=1`, U-W15-T16).
- **What's missing:** any e2e-reachable way to force a unit to go scarce between order creation and payment verify. Read-only verified 2026-10-18: SPEC-6-02 made `stock` read-only on REST product updates (`backend/products/serializers.py` — `if self.instance is not None: self.fields["stock"].read_only = True`), so the spec's staff `PATCH /api/products/marj/ {stock: 0}` is accepted but silently does not mutate stock; the sanctioned mutation is the admin-only `adjust_stock` (delta + StockMovement ledger), with no REST surface.
- **Why:** the captured-but-unfulfillable 409 branch (`PaymentLauncher` `captured-dead-end`) is a real verify-time path, but an automated test cannot reach it without either a writable stock seam or a fixture product whose stock the test can pin to 1 and then double-buy. The e2e documents this seam rather than asserting a green it cannot prove.
- **Suggested contract:** either (a) a capability-gated `POST /api/products/{id}/adjust-stock/` accepting `{delta, reason}` (ledger-written through `adjust_stock`, same capability `inventory.adjust` as the admin action), or (b) one fixture product with `stock = 1` created by the e2e seed so the test can deterministically lose the race via a second session checkout.
- **Current workaround (if any):** the 409 test stays behind `RAZORPAY_TEST=1` with an in-file comment naming the seam; the dead-end UI itself is unchanged and the pre-payment e2e never depends on it.

## [P2] Reproducible e2e seed (the storefront e2e fixtures exist only in a hand-seeded dev DB)
- **Where it's needed:** all of `frontend/e2e/*` (checkout, orders).
- **What's missing:** any in-repo seed for the e2e fixture rows the specs bind to: user `e2e_customer` (+ `e2e_staff`), product slug `marj`, coupon `E2ECHECK10`. Read-only verified 2026-10-18: `manage.py seed_demo` (`backend/orders/management/commands/seed_demo.py`) creates only `demo-`-prefixed users/products and `DEMO-`-prefixed coupons — none of the e2e rows. They were hand-created on the E2E-1 run's DB (ledger row E2E-1: "Seed subagent: e2e_customer valid, product 4 name=marj, coupon E2ECHECK10 window extended").
- **Why:** a fresh database cannot run the storefront e2e suite at all (login 401 / product 404 / coupon 400), so the suite's reproducibility depends on undocumented manual state — a silent trap for the next owner who tries to close BUG-4.
- **Suggested contract:** a `seed_e2e` management command (or documented fixture set) creating exactly: one active non-privileged customer, one staff user, one in-stock product at a known price, and one long-window percentage coupon — idempotent and marked like `seed_demo` (`e2e-`/`E2E-` prefixes) so `--cleanup` semantics stay consistent.
- **Current workaround (if any):** the specs derive every money expectation from whatever the API serves (U-W15-T16), so price drift no longer breaks them — but the rows themselves must still exist; the operator runbook for a backend-up e2e pass is to seed them by hand as the E2E-1 pass did.

## [P1] `POST /api/accounts/logout/` - refresh-token revocation (GATE-U U1-04, compliance pass 2026-10-18)
- **Where it's needed:** header sign-out (rontend/src/app auth context - `frontend/src/lib/auth.tsx:95` best-effort POSTs the endpoint today).
- **What's missing:** any logout route. Verified read-only 2026-10-18 against `backend/accounts/urls.py:21-52`: login/refresh/register/recovery exist; NO logout. The HttpOnly refresh cookie set by Q-P1 is never revoked server-side; U1-04 measured the live 404 + Sign-out header persisting until reload.
- **Why:** SPEC-17.12/R-17.12 refresh-cookie contract is half-built without revocation - a signed-out session's refresh token stays valid until expiry (90d default), so sign-out cannot actually end the session server-side.
- **Suggested contract:** `POST /api/accounts/logout/` (authenticated + CSRF) -> blacklist the refresh token from the cookie, clear the cookie (path=/api/, matching Q-P1's set), return 204 with no body oracle. Client already clears local state regardless, so the endpoint is purely the server half.
- **Current workaround:** frontend clears local state + cookie hint; refresh cookie lives until max-age expiry. BACKEND_REQUESTS P1.


## [P1] Order number generation - randomized, not padded sequential (user finding 2026-10-18)
- **Where it's needed:** orders list/detail, tracking, admin
- **What's missing:** order numbers are sequential padded (`2026000003` for the 3rd order) instead of randomized/unpredictable.
- **Why:** sequential padded numbers leak order volume and are guessable; user expects randomized order numbers.
- **Suggested contract:** generate order number with a random component (e.g. `ORD-<random>` or UUID-derived) at order creation; keep a stable display format.
- **Current workaround:** frontend displays whatever `order_number` the API returns.

## [P0] Admin panel - address not found / broken order lifecycle (user finding 2026-10-18)
- **Where it's needed:** Django admin orders view
- **What's missing:** shipping/billing address not visible in admin; order lifecycle transitions broken.
- **Why:** admin cannot fulfill/support orders without address; lifecycle (pending->paid->shipped->delivered->refunded/cancelled) must be operable.
- **Suggested contract:** expose address fields on OrderAdmin; implement/fix status transition actions with guards.
- **Current workaround:** none - flagged as P0.

## [P1] Product image upload - admin allows only 1 upload but populates both image fields (user finding 2026-10-18)
- **Where it's needed:** product admin + PDP gallery
- **What's missing:** admin upload accepts a single image that is written to both image fields; no multi-image support.
- **Why:** PDP can show multiple images per product but admin can only supply one.
- **Suggested contract:** ProductImage model (FK to Product, ordering, alt) with inline admin; or accept multiple uploads and store as list.
- **Current workaround:** frontend shows whatever images the API returns (single).

## [P1] Product variant images - no option to add images for variants (user finding 2026-10-18)
- **Where it's needed:** variant admin + PDP variant selector
- **What's missing:** variant model/admin has no image field.
- **Suggested contract:** add `image` (FK/URL) to ProductVariant; admin inline upload.
- **Current workaround:** none.

## [P1] Product variants API surfacing (user finding 2026-10-18)
- **Where it's needed:** PDP variant selector, cart
- **What's missing:** verify `/api/products/{slug}/` (or variants endpoint) returns variants with price/stock/image; if absent, add.
- **Why:** variants are not surfaced in the UI.
- **Suggested contract:** include `variants: [{id, name, price, stock, image}]` in product detail payload.
- **Current workaround:** frontend hides variant selector if no variants.

## [P1] Refund endpoint for a product/order (user finding 2026-10-18)
- **Where it's needed:** refund page + order lifecycle
- **What's missing:** no refund API (create refund request, status, admin approval).
- **Why:** there is no page to refund a product; lifecycle includes refunded state.
- **Suggested contract:** `POST /api/orders/{id}/refund/` (or `/api/refunds/`) with reason + items; admin approve/reject; status transitions.
- **Current workaround:** none - frontend refund page blocked on this.

## [P2] Order lifecycle status definitions (refunded/cancelled on tracking) (user finding 2026-10-18)
- **Where it's needed:** tracking page, orders list
- **What's missing:** documented status enum + transitions; frontend unsure whether to show refunded/cancelled sections.
- **Why:** tracking page shows refunded/cancelled sections; user questions their presence.
- **Suggested contract:** publish status enum + allowed transitions; frontend renders only applicable states.
- **Current workaround:** frontend renders all statuses returned.
