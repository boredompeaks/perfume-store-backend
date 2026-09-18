# Frontend Plan — Perfume Store

Status: **awaiting approval**. No frontend code written yet.
Backend: Django/DRF at `backend/` (read-only). Verified against code 2026-09-18.

---

## 1. Verified API surface (from code, not docs)

Base URL: `NEXT_PUBLIC_API_BASE_URL` (dev: `http://localhost:8000`).
**All requests send `credentials: 'include'`** — the cart is a Django session cookie,
and checkout is broken without it (`CORS_ALLOW_CREDENTIALS=True` on the backend).
**Same-site constraint (found live in phase 5 browser testing):** the API origin must be
**same-site** with the frontend origin (e.g. `localhost:8000` + `localhost:3000`, or
`api.brand.com` + `www.brand.com` — ports don't split a site, but `127.0.0.1` vs
`localhost` does). Chrome's Lax-by-default cookie handling does not attach the
`sessionid` cookie to cross-site `fetch` calls, which silently empties the session cart
while CORS and JWT keep working. Production deployment must keep frontend and API
same-site (subdomains of one registrable domain, or one origin behind a reverse proxy).

### Products (public)
- `GET /api/products/?search=&category=&min_price=&max_price=&ordering=&page=`
  → `{count, total_pages, current_page, next_page: bool, previous_page: bool, results: Product[]}`
  - **Page size is hardcoded to 2** (F-23 dev leak — flagged in BACKEND_REQUESTS.md; UI must use page-number pagination, not infinite scroll).
  - No default ordering → page drift possible (F-12).
  - `ordering` whitelist: `price, -price, name, -name, created_at, -created_at`.
  - Invalid `min_price`/`max_price` → `400 {error}`.
- `GET /api/products/{slug}/` → `Product` | `404 {error}`
- `Product`: `{id, name, slug, description, price: "1234.00" (string), size: int (ml), stock: int, category: string, image: url|null, created_at}`
  - `image` is a **single** ImageField (no gallery). Serializer has no request context → **relative** `/media/...` paths; frontend prefixes the API origin. Missing image → placeholder component.
- **No categories endpoint.** Workaround: derive/hardcode; see BACKEND_REQUESTS.md.

### Cart (session cookie, no JWT)
- `GET /api/cart/` → `{id, session_id(ignored), items: [{id, product: Product, quantity, created_at}], ...}` — lazily creates session + cart.
- `POST /api/cart/ {product_id, quantity}` → `201` full cart | `400 {error}` ("Not enough stock" etc.) | `404`
- `PATCH /api/cart/{item_id}/ {quantity}` → `200` full cart | `400 | 404`
- `DELETE /api/cart/{item_id}/` → **200** with full cart (not 204).
- **Cart response contains no totals.** Displayed subtotal is a client-side estimate; server truth arrives at checkout / apply-coupon.

### CSRF status (verified in code)
`@api_view` marks every view `csrf_exempt`, and `REST_FRAMEWORK` configures **only** `JWTAuthentication` — `SessionAuthentication` never runs, so DRF's `enforce_csrf` never fires. **Cart mutations are not CSRF-enforced server-side** (the docs' "session-cart endpoints are covered" claim does not hold against code). An API-only backend also has no CSRF-cookie issuer, so the frontend cannot send a token today. Mitigation relied on: Django session cookies are SameSite=Lax-by-default in modern browsers, which blocks cookie attachment on cross-site form POSTs — but that is browser behavior, not server enforcement. Logged as a **P1** BACKEND_REQUESTS item; the frontend will send `X-CSRFToken` the moment a CSRF cookie exists.

### Orders (JWT required; session cookie also required for checkout)
- `GET /api/orders/` → `Order[]` (unpaginated, newest first)
- `POST /api/orders/checkout/ {full_name, phone, address, city, state, pincode, coupon_code?}`
  → `201 Order` | `400 {error}` (missing field / coupon rejections / empty cart) | **404 "Cart not found" when the session cookie is missing/expired**
  - `Order`: `{id, user, full_name, phone, address, city, state, pincode, status, coupon: code|null, discount_amount, total_amount, items: [{id, product, product_name, price, quantity, subtotal}], created_at, updated_at}`
- `POST /api/orders/apply-coupon/ {code}` (session only, no JWT) → `200 {coupon, subtotal, discount, final_total}` | `400 {error}` or `{error, minimum_order_amount}`
  - Unthrottled server-side (V-11) → frontend calls it only on explicit "Apply", never per keystroke.
- `POST /api/orders/payment/ {order_id}` → `{order_id, razorpay_order_id, amount (paise), amount_in_rupees, currency, key_id}` — idempotent per order; `400` if not pending.
- `POST /api/orders/payment/verify/ {razorpay_order_id, razorpay_payment_id, razorpay_signature, order_id}`
  → `200 {message, order_id, status, razorpay_payment_id}` | `400` (bad signature / already processed / mismatch) | **409 stock/coupon failure *after* money captured** (V-03 — no refund, no webhook). This client call is load-bearing; its failure path must be visible (see §8).

### Accounts
- `POST /api/accounts/register/ {username, email, password}` → `201 {message, user}` | `400` field errors | `503` SMTP failure (account *was* created).
- `POST /api/accounts/login/ {username, password}` → `{access, refresh}` | `401` (generic — wrong creds **or** unverified; indistinguishable by design).
- `POST /api/accounts/token/refresh/ {refresh}` → `{access}`.
- `POST /api/accounts/verify-email/ {uid, token}` → `200` | `400 {error}`
- `POST /api/accounts/resend-verification/ {email}` → uniform `200`
- `POST /api/accounts/forgot-username/ {email}` → uniform `200`
- `POST /api/accounts/password-reset/ {email}` → uniform `200`
- `POST /api/accounts/password-reset/confirm/ {uid, token, password}` → `200` | `400 {error}` or `{password: [msgs]}`
- `GET /api/accounts/username-available/?username=` → `{available, message}`
- **Email links point at `{FRONTEND_URL}/verify-email?uid=…&token=…` and `{FRONTEND_URL}/reset-password?uid=…&token=…`** → these two routes must exist at exactly those top-level paths (default `FRONTEND_URL` is `http://localhost:3000`).
- SimpleJWT is **unconfigured** → ~5-min access tokens → silent refresh on 401 is mandatory.

---

## 2. Stack

Next.js (App Router, TypeScript) · Tailwind CSS · TanStack Query v5 (server state) · `next/image` · `next/font`.
No Zustand (one small auth context suffices — no cart store mirrors money/stock; the cart is server state in Query's cache). No Framer Motion (see §5). Razorpay Checkout.js loaded **only** on the payment step.

Rendering: PLP/PDP are server components rendering on demand with `revalidate = 60` fetch caching. Home renders on demand (`force-dynamic`) — **a deliberate, standing choice, not a stopgap**: build-time prerendering must not depend on Django being reachable from the build runner, and the current deployment story has no edge/CDN layer where ISR would pay off; the 60s data cache bounds backend load. Revisit trigger (documented so it can't silently calcify): if an edge/CDN deployment lands AND builds can guarantee backend reachability (or deploy-time revalidation is added), revert Home to ISR. Cart/checkout/auth/orders are client components. PDP metadata (title/OG/JSON-LD) generated server-side.

---

## 3. Information architecture (routes)

| Route | Purpose | Auth |
|---|---|---|
| `/` | Hero (static composition, no carousel), New Arrivals (API), brand editorial. No fabricated testimonials/badges. | public |
| `/products` | PLP; all filters live in URL params (`?search=&category=&min_price=&max_price=&ordering=&page=`) → shareable/SEO-clean | public |
| `/products/[slug]` | PDP: image, price, size (ml), stock indicator, add-to-cart, description, JSON-LD | public |
| `/cart` | Line items, qty steppers, remove, coupon preview, subtotal estimate, CTA: "Sign in to checkout" (guest) / "Proceed" | public |
| `/checkout` | 3 steps in one page: Address → Review & coupon → Pay. Pay step creates order → Razorpay → verify | JWT |
| `/orders` | Order history (account home after login) | JWT |
| `/orders/[id]` | Order detail; doubles as payment-confirmation landing (`?payment=success`) and the post-payment failure states | JWT |
| `/login` | JWT login; on 401 shows generic error **plus** "didn't verify? resend" / "forgot username" links (backend is enumeration-safe, 401 is ambiguous) | public |
| `/register` | Register + live username availability check (debounced) | public |
| `/verify-email` | Email-link landing: parses `uid`/`token`, POSTs, shows success/expired states with resend path | public |
| `/reset-password` | Email-link landing: `uid`/`token` + new-password form (server `validate_password` errors shown per-field) | public |
| `/forgot-username` | Email lookup form | public |
| `not-found.tsx` / `error.tsx` | Branded 404 / error boundary with retry | — |

Deliberately omitted: `/account` profile page (no profile endpoint exists), category landing pages (no categories endpoint).

---

## 4. Navigation pattern — decided

- **Mobile: hamburger-triggered full-screen overlay menu.** Why: the nav surface is small (Shop, New, About-less), a full-screen overlay lets the menu carry the brand's typographic/photographic language (Aesop/Le Labo pattern) with simpler focus management than an off-canvas drawer.
- **Desktop: fixed top bar** — links left, wordmark center, search + account + cart right. No mega-menu (few links, no taxonomy to expose).
- **Cart affordance: persistent header icon with count badge**, navigates to `/cart`. On add-to-cart: button success state + toast with "View cart" action. **No slide-in drawer** — cart edits (stock errors, quantity limits) are real interactions that would duplicate the `/cart` surface; one well-built page beats two half-built ones.
- Mobile PDP gets a **sticky bottom add-to-cart bar** (price + button) — purposeful, not decorative.

---

## 5. Component inventory (with states)

| Component | States |
|---|---|
| `Header` | logged-in (Orders link) / guest (Sign in); cart count; menu overlay open |
| `Footer` | static |
| `ProductCard` | loading (skeleton), image-missing placeholder, out-of-stock ("Sold out" overlay) |
| `ProductGrid` | loading skeletons, empty ("no results" + clear filters), error w/ retry |
| `Pagination` | page numbers from `total_pages`/`current_page`; disabled edges |
| `FilterBar` (search, category, price, sort) | syncing to URL; invalid price → inline error (400 shape) |
| `QuantityStepper` | min 1, max = stock (stock error surfaces server "Not enough stock") |
| `AddToCartButton` | idle / adding / added (✓ 1.5s) / out-of-stock / error |
| `CartLineItem` | updating (qty PATCH in flight), removing (fade), server-error banner |
| `CartSummary` | subtotal (estimate), coupon applied preview, server-reconciled totals at checkout |
| `CouponForm` | idle / applying / applied (`{coupon, discount, final_total}`) / invalid / min-order-not-met (shows `minimum_order_amount`) |
| `CheckoutSteps` | step indicator; per-field 400 error mapping (`full_name is required` → field) |
| `PaymentPanel` | creating order / launching Razorpay / verifying / success / dismissed ("Payment not completed — Retry") / **verify-failure incl. 409 money-captured state** |
| `OrderStatusBadge` | pending/confirmed/shipped/delivered/cancelled (neutral palette, no fake green "trust" styling) |
| `OrderSummary` | items with snapshot prices, discount, total |
| `AuthForm` (login/register/reset) | submitting / field errors (DRF shape) / uniform-200 messaging for anonymous flows |
| `Toast` | `aria-live` region; auto-dismiss 4s; reduced-motion aware |
| `EmptyState`, `ErrorState`, `Skeleton*` | shared |

## Animation inventory

Framer Motion: **deliberately not used** — nothing needs layout/gesture orchestration; a 0-JS animation layer keeps the bundle lean. All motion is CSS.

| Element | Trigger | Technique | Duration/easing | Reduced motion |
|---|---|---|---|---|
| Menu overlay | open/close | CSS opacity+translate | 200ms ease-out | instant |
| Add-to-cart success | state change | CSS bg/color transition | 150ms | instant |
| Toast enter/exit | mount/unmount | CSS keyframe slide+fade | 200ms | fade only |
| Cart line remove | DELETE resolve | CSS height/opacity transition | 200ms | instant |
| PDP image hover | pointer | CSS scale 1.02 | 300ms | off |
| Skeleton shimmer | loading | CSS keyframe | 1.2s loop | static gray |
| Global | — | `@media (prefers-reduced-motion: reduce)` kill-switch in CSS | — | enforced |

## Responsive breakpoints (concrete layout differences)

| Breakpoint | Layout |
|---|---|
| `<640px` phone | Full-screen menu; 2-col product grid; PDP stacked w/ sticky add-to-cart bar; cart lines stacked (thumb left, controls right); checkout single column, steps as progress dots + one visible step; order cards stacked |
| `640–1023px` tablet | 2–3-col grid (sparse at page size 2 — see BACKEND_REQUESTS); PDP 2-col (image / info); header condensed nav; checkout 2-col (form / summary) |
| `≥1024px` desktop | Inline nav; 4-col product grid in `max-w-6xl`; PDP 2-col with **sticky** info panel; 12-col section rhythm, generous whitespace |

## Gesture inventory

Minimal by design: **no swipeable gallery and no pinch-zoom — the product model has a single image** (flagged for multi-image in BACKEND_REQUESTS). No drag-to-reorder (a 5-item cart doesn't need it). Gestures that remain: native scroll, tap-to-close on the menu overlay, Esc/focus-trap in the menu. Everything else is buttons.

---

## 6. SEO plan

- **Title template:** `%s — Maison Aurel` (placeholder brand; single `site.ts` config to rename). Home: `Maison Aurel — Perfumes composed with restraint`. PDP: `${name} — Eau de Parfum ${size} ml`.
- **Descriptions:** PDP uses trimmed `description`; PLP static template; filtered PLP views (any param beyond `page`) get `noindex` to avoid crawl-trap faceted URLs; canonical URLs on all public pages.
- **Open Graph:** default static OG for home/PLP; PDP OG uses product image + price.
- **JSON-LD:** `Organization` (home), `Product` + `Offer` (PDP: price INR, `priceCurrency`, `availability` from stock>0), `BreadcrumbList` (Home → Products → Name).
- **`sitemap.ts`:** static routes + all product slugs fetched from the API (all pages), `revalidate` 1h. Note: walking all pages at the current hardcoded page size of 2 costs ~`count/2` API calls per regeneration — a temporary cost that collapses once the P0 page-size fix lands (BACKEND_REQUESTS). **`robots.ts`:** allow all; disallow `/checkout, /orders, /login, /register, /verify-email, /reset-password`.
- `next/image` + next/font keep Core Web Vitals (LCP/CLS) inside budget below.

## Performance budget

- Lighthouse (mobile): Perf ≥ 85, A11y ≥ 95, Best Practices ≥ 95, SEO 100.
- First-load JS: **≤ 120 KB gzipped** on Home/PLP/PDP. No animation lib; Razorpay script (~50 KB) injected only when the payment step mounts; TanStack Query is the only state dependency.
- Fonts: `next/font/google` — **Fraunces** (variable, display; tight tracking, characterful serif) + **Source Sans 3** (body/UI). Self-hosted at build time, `display: swap`, latin subset — zero third-party font requests.
- Images: `next/image` with `remotePatterns` for the API origin; explicit aspect-ratio boxes (no CLS); graceful placeholder for missing product images.

## Testing plan (free tooling: Vitest + Playwright)

- **Vitest unit:** API client (401 → single-flight silent refresh → retry → logout), money formatter (`en-IN`, INR, API strings), PLP URL-param ↔ filter parsing round-trip, coupon form state machine (applied/invalid/min-order), auth token storage.
- **Playwright e2e** (against seeded Django dev server; Razorpay keys from backend `.env`, test mode):
  1. PLP: search + category + price + ordering + pagination shape.
  2. Cart: add, quantity accumulate, PATCH, DELETE, over-stock 400 surfacing.
  3. Coupon: apply valid / invalid / min-order paths; preview math rendered from server response.
  4. Checkout: happy path **up to the Razorpay handoff** (assert order created + modal launches) — no iframe automation.
  5. Form validation: register/login/checkout required-field 400 mapping.
  6. Auth guard: `/checkout`, `/orders` redirect anonymous → login → return.
- **Explicitly NOT tested:** purely presentational components (Footer, badges, empty-state art) — no logic, low risk; testing them adds maintenance without catching regressions.

## Accessibility baseline

Landmarks + skip link; full keyboard nav; focus trap + Esc + focus return in menu overlay and payment modal; visible 2px offset focus rings (bronze accent, ≥3:1); body text contrast ≥ 4.5:1 (luxury palettes drift low-contrast — this is a hard gate); `aria-live` for toasts and cart-count changes; form errors linked via `aria-describedby`; `prefers-reduced-motion` global guard.

---

## 7. State & money rules (backend truth)

- Cart lives in TanStack Query cache (`['cart']`), invalidated after every mutation. **No client-side cart store.**
- Subtotal shown on `/cart` is a **display estimate**; checkout/coupon totals always render from server responses. Once an order exists, `Order.total_amount` is the only number shown.
- JWT: access token in memory only; refresh token in `localStorage`; single-flight refresh on 401; failed refresh → logged-out state. Refresh-token-in-`localStorage` should move to an httpOnly cookie if the backend ever supports it (tracked via the SIMPLE_JWT configuration request).
- Coupon code is kept in client state and sent as `coupon_code` at checkout (apply-coupon is only a preview endpoint).
- Checkout address prefill via `localStorage` is **PII handling, not just UX** (phase-5 requirement, built in from the start): a visible "saved on this device" disclosure at the point of prefill, an explicit "forget saved details" control on the address step, and the key cleared on logout.
- Phase-5 live-testing amendments (all driven by real browser evidence, disclosed): (1) checkout collects **email** — required for receipts and needed for complete Razorpay prefill (the app has no `/me` endpoint to source it); (2) **pending orders are resumable** — order detail shows "Complete payment" (reusing `PaymentLauncher` with the idempotent `payment/` endpoint), because a user who refreshes or navigates away mid-payment would otherwise orphan a pending order with no pay path; this is phase-6 scope pulled forward by test evidence.

## 8. Payment flow & failure paths (load-bearing)

```
Pay → POST /orders/checkout/ (server total, 201 Order)
    → POST /orders/payment/ {order_id} → {key_id, razorpay_order_id, amount}
    → inject checkout.js → open modal (prefill name/email/phone, brand theme)
    → handler → POST /orders/payment/verify/ {…, order_id}
        200 → invalidate cart → /orders/{id}?payment=success
        400 → visible error; "Retry payment" (payment creation is idempotent)
        409 → **"Payment was captured but the order could not be fulfilled — contact support"** (money-captured path; no silent retry, no black hole)
    → modal dismissed → "Payment not completed — order saved as pending; Retry payment or view in Orders"
```

No webhook exists (V-03): a closed tab leaves a paid-order-in-`pending` risk server-side; the frontend makes every verify failure explicit and keeps pending orders recoverable from `/orders`.

## 9. Error-handling semantics (global)

- `401` (orders) → silent refresh once → retry → else logout + redirect.
- `404` on checkout/apply-coupon ("Cart not found") → "Your cart session expired — add items again" + link to `/products` (session-cookie reality, not a bug to paper over).
- `400` field errors → mapped onto form fields; unknown shapes → inline alert.
- `503` SMTP states → "Account created — we couldn't send the email; use Resend verification."
- No error is swallowed; no client-side retry of money mutations without user action.

### Known limitations (accepted, documented — found in live phase-5/6 testing)

- **Stock/price display can lag ≤60s** (products fetch cache, `next.revalidate`). Server truth is enforced at cart-add, checkout, and payment-verify, so staleness can never overcharge or oversell — it only means a card can briefly say "In stock" and fail at add time.
- **Razorpay 2.0 widget blocks untrusted close attempts** (its `ondismiss` cannot be triggered by automation; its mobile validator also rejects sequential numbers like `9876543210`). E2E uses the wallet route; the forced-failure test abandons mid-payment and resumes from the order page, which is the real recovery path for stuck payments.
- **Session cart requires same-site origins** (see §1 and BACKEND_REQUESTS) — enforced via env/config, not fixable in code.
- **Orders list is unpaginated** server-side; fine at current volume, revisit with the single-order endpoint request.

## 10. Build sequence (after approval)

1. ✅ Scaffold Next.js + Tailwind tokens (palette/type/spacing), fonts, `site.ts`, layout, Header/Footer, API client + auth context + TanStack Query provider.
2. ✅ Products: PLP (filters/pagination/skeletons) + PDP (JSON-LD, metadata, add-to-cart).
3. ✅ Cart page + mutations + toasts.
4. ✅ Auth pages (register/login/verify-email/reset-password/forgot-username) incl. email-link landing routes.
5. ✅ Checkout: address → review/coupon → payment (Razorpay) → verify → confirmation states — verified live in Razorpay test mode (happy path, abandoned-payment idempotency, 409 dead end).
6. ✅ Orders list/detail (+ pending-order resume pulled forward from live-test evidence).
7. SEO extras (sitemap/robots/OG), a11y pass, Vitest + Playwright setup (Playwright pulled forward to phase 5), perf pass, `BACKEND_REQUESTS.md` upkeep.
