# UI/UX specification — perfume-store storefront

> **STATUS: FILLED by `design-research`, 2026-10-02.** Every placeholder marker
> is resolved and no dispatch blocker remains. Sources read in full before writing: `frontend/src/**` (all 22 route
> files, 39 components, 22 lib modules), `frontend/PLAN.md`,
> `frontend/next.config.ts`, `backend/config/urls.py` + every app `urls.py`,
> `backend/orders|products|cart|shipping|accounts|ops` views and serializers,
> `backend/docs/conventions.md`, `backend/docs/api-contract.md`,
> `BACKEND_REQUESTS.md`, and spec §3, §4, §13, §15, §18–19 of
> `Saturday, Jul 25, 2026 at 5_23 AM.txt`.
>
> **Depth bar: `backend/docs/conventions.md`.** That file is the standard. A
> section here that is hand-waving where `conventions.md` is specific is not done.
>
> Owner: `design-research`. Consumer: `frontend-architect`.
>
> **§14 is not mine.** The reconciliation checklist is reproduced byte-for-byte
> from the skeleton. It is executed by the backend orchestrator.

---

## Read this first: the frontend is behind its own backend

One finding outranks every design decision in this file, and it is verified
from source rather than inferred.

**The backend grew six customer-facing capabilities that the storefront does
not consume, and one of them makes a currently-shipped UI decision wrong.**
Confirmed by reading the urlconfs, views and serializers directly:

| Backend capability (verified) | What the storefront does today | Consequence |
|---|---|---|
| `POST /api/orders/checkout/` is `@permission_classes([AllowAny])`; mints `order_number` and, for a userless order, a `guest_token` returned **once** in the checkout response (`orders/views.py:600`, `:1067`, `:289`) | `CheckoutView.tsx` renders a "Sign in to check out" wall for every anonymous visitor | **A supported flow is blocked.** Checkout must offer guest purchase (spec §4.2: "Checkout may support guest purchases"). See §5.4, §12-G1. |
| `GET /api/shipping/track/<order_number>/` — `AllowAny`, credential = `X-Guest-Order-Token` or an authenticated account, returns `{shipments: [{order_number, status, carrier, tracking_number, estimated_delivery, events: [{status, occurred_at}]}]}` (`shipping/views.py:177`, `serializers.py:56`) | No tracking surface exists at all | Spec §3.9 names `/track-order`. It is now buildable, and it is the single highest-value new page. See §5.6, §12-G2. |
| `GET /api/shipping/estimate/?state=&pincode=` → `{state, pincode, currency, options: [{method, name, amount, free_shipping}]}`; `AllowAny`; 400 on an unserviceable destination (`shipping/views.py:36`) | Cart says "Final totals are confirmed at checkout"; checkout shows no shipping line at all | Delivery is now **priced**. A checkout that cannot show the delivery price before payment is a spec §3.7 failure. See §5.4, §12-G3. |
| `OrderSerializer` carries `order_number`, `payment_status`, `fulfilment_status`, `payment_method`, `shipping_method`, `shipping_amount`, `shipping_method_code`, `currency`, and six lifecycle timestamps (`orders/serializers.py:83`) | `src/lib/types.ts` `Order` has **none** of them; the detail page renders one `status` badge | The order page is throwing away the backend's own lifecycle model. `types.ts` is stale. See §5.5, §12-G4. |
| `POST /api/cart/coupon/` + `DELETE /api/cart/coupon/` — coupon persisted on the cart row, idempotent, uniform 400 (`config/urls.py` cart family) | `CouponForm.tsx` calls the **stateless preview** `/api/orders/apply-coupon/` and parks the code in `sessionStorage` | A hard refresh loses the applied coupon, and the code must be re-sent as `coupon_code`. The persisted endpoint exists. See §5.3, §12-G5. |
| `POST /api/products/<slug>/restock-notifications/` — customer back-in-stock opt-in/out (`products/urls.py`) | Nothing | The correct, real replacement for the "customers also bought" carousel this design bans. See §5.2, §12-G6. |
| `GET /api/orders/<order_id>/` — owner-scoped single order (`orders/urls.py`) | `OrderDetailView` fetches the **whole** order list and `.find()`s the one row | Spec §13: "Use server-side data fetching for catalogue pages where it benefits performance". One order should cost one request. See §5.5. |

Also verified, and it changes two other things:

- **`PRODUCTS_PAGE_SIZE = 12`** (`config/settings.py:540`), not the hardcoded 2.
  The sitemap comment about walking "`~count/2` requests" and the PLP's
  2-per-page degradation note are both obsolete.
- **The product image is still a single `ImageField`**
  (`products/serializers.py`), there is still **no categories endpoint**, and
  there is still **no `in_stock` flag**. So: no gallery, no category tiles, no
  star ratings, no "shop by note". The negative lists in this spec are not
  taste — they are what the data model permits.
- **The Razorpay webhook now exists** (`/api/v1/webhooks/razorpay/`,
  `config/urls.py`). `BACKEND_REQUESTS.md`'s P0 "no webhook" entry is stale.
  The *client* half of that P0 is not resolved: a 409-after-capture order is
  still indistinguishable from a never-paid one in `GET /api/orders/`. See
  §12-G7.

**P0 blocker, record only:** `frontend/next.config.ts` ships
`script-src 'self' 'unsafe-inline'` with no `https://checkout.razorpay.com` and
no `frame-src`. `src/lib/razorpay.ts:19` injects that script. **The payment step
cannot load the gateway under the CSP the app already sets.** This is recorded
in §12-G8 and must be fixed in config, not worked around in the client.

---

## 0. Design direction — settled

**Restrained-luxury base, theatrical motion as accent.** Not a compromise.

- Restraint is what reads as expensive. Luxury perfume cannot carry the visual
  noise of a template-driven DTC page. Whitespace, hierarchy and one confident
  type pairing *are* the luxury.
- Motion earns its place only where it **communicates state** or **directs
  attention through narrative**. Motion that exists for its own sake is a defect.
- The accent is concentrated: one or two signature moments per page, executed
  well, rather than everything animated.

### CSS-first is an engineering position, not a preference

Reach for the platform before a dependency, in this order:

1. **CSS-only, compositor-friendly.** `transform` and `opacity` only. Never
   animate `width`, `height`, `top`, `left`, `margin`, or `box-shadow` in a way
   that triggers layout or paint per frame.
2. **CSS-native modern features.** `@starting-style`, view transitions,
   `animation-timeline: view()` / `scroll()`, `content-visibility`.
3. **A JS library only when orchestration is genuinely required** — layout
   animation, gestures, shared-element transitions. Name it, state the KB, and it
   must be **route-scoped**, never global.
4. **GSAP + ScrollTrigger** only for scrubbed timeline sequences CSS cannot
   express. Same rule: route-scoped.

The client asked for animation-heavy **and** heavily optimised. Those are in
tension. This spec resolves the tension rather than ignoring either half:
nothing above the fold animates before LCP; static HTML reaches crawlers;
`prefers-reduced-motion` is honoured at the token layer, not per component; and
every recommendation carries its cost so §10 can be enforced.

### What already exists and must not be regressed

The current frontend is a working storefront with a real design system. This is
a **redesign, not a rewrite from nothing**. Every existing asset was read and is
recorded below as KEEP, CHANGE or DROP. Verified against source on 2026-10-02.

### KEEP — survives the redesign unchanged, and why

| Asset | Where | Why it survives |
|---|---|---|
| Palette `paper #faf8f3` / `surface #fff` / `ink #1c1917` / `ink-muted #57534e` / `bronze #8a5a24` | `globals.css:5-15` | Warm ivory + ink + one bronze is the correct luxury register. Every text pair passes AA with margin (§2.1). Re-picking a palette is churn with no gain. |
| `line #e6e1d7` **and** `line-strong #75706a` as two separate tokens | `globals.css:9,13` | The split is the point: `line` is decorative (1.23:1, dividers), `line-strong` meets 1.4.11 non-text (4.62:1, fields). Collapsing them into one token reintroduces the bug `ui.test.ts` pins. |
| `inputClass` in `lib/ui.ts` | `lib/ui.ts:16` | The single source of truth for field styling, and the reason the placeholder/border AA fix landed everywhere at once. `ui.test.ts` pins it. |
| `:focus-visible { outline: 2px solid bronze; outline-offset: 2px }` | `globals.css:25` | Correct shape and offset. Only the **colour** changes (§2.1 `--focus`). |
| Skip link, `sr-only focus:not-sr-only` → `#main` | `layout.tsx:52` | Already correct: focusable, on `:focus-visible`, targets a real `<main id="main">`. |
| Menu-overlay focus trap: initial focus, Tab cycling, Shift-Tab wrap, Escape, focus restore, `body` scroll lock | `MenuOverlay.tsx:26-64` | A genuine trap, hand-rolled, ~35 lines, zero dependencies. Spec §15.4 "Dialog focus management". KEEP AS IS. |
| Global `prefers-reduced-motion` kill-switch | `globals.css:117-125` | Keep as a **backstop**. But see CHANGE-5: it is insufficient on its own for scroll-driven animations, and this redesign adds them. |
| `Fraunces` (display) + `Source Sans 3` (text) via `next/font/google`, `subsets:["latin"]`, `display:"swap"` | `layout.tsx:9-19` | The pairing is right and it is already self-hosted at build time. See §2.2 for what to add, not what to replace. |
| Type in the sans for all UI, serif for all headings and money-adjacent display | every component | Correct split. Nothing changes. |
| Corner language: **square (0px)** everywhere except the cart-count counter | all components | The single strongest luxury signal in the current build. Reinforced in §2.3. |
| `.link-underline` — `scaleX(0→1)` underline, `transform-origin` flipping right→left | `globals.css:70-89` | Compositor-only (`transform`), correct origin flip on enter/exit, and it respects the reduced-motion backstop. Textbook. KEEP. |
| `.noise::before` — inline-SVG fractal noise at alpha 0.05 | `globals.css:109` | `aria-hidden`-equivalent (a pseudo-element), no network request, no layout. The one piece of texture in the build and it is invisible-until-you-look. KEEP. |
| `.text-ghost` outlined watermark | `globals.css:103` | Decorative only, `aria-hidden`, never carries information (§2.1 exemption). KEEP. |
| `toJsonLdScriptContent` — `< > &` and U+2028/29 escaping | `lib/json-ld.ts:1` + `json-ld.test.ts` | A real, tested XSS defence for staff-controlled product text. Any JSON-LD added by this redesign goes through it. KEEP. |
| `ProductImage` null-path and 404 fallback to a typographic monogram | `ProductImage.tsx:32` | The product model permits exactly one image and media 404s are a live production risk. The monogram is the correct quiet failure. KEEP. |
| `ProductCard` 4:5 aspect box with `bg-bronze-soft` behind the image | `ProductCard.tsx:20` | CLS-safe by construction — the box is sized before the bytes arrive. §10 depends on it. KEEP. |
| `GridSkeleton` mirrors the real grid's exact columns per breakpoint | `GridSkeleton.tsx:4` | A skeleton that is not the same shape as the content causes the CLS it was meant to prevent. KEEP. |
| `toast` — `aria-live="polite"` container, max 3 visible, 4s auto-dismiss | `toast.tsx` | Announcement-before-animation ordering is right: the live region is on the container, not on the animated child. KEEP. |
| Cart count `aria-live="polite"` + `aria-label="Cart, N items"` | `Header.tsx:144-155` | Spec §15.4 "Screen-reader announcements for cart updates". KEEP. |
| `GuestOnly` redirect, `safeNext()` open-redirect guard | `GuestOnly.tsx`, `redirect.ts` | Security, not design. KEEP. |
| `PaymentLauncher`'s `captured-dead-end` phase | `PaymentLauncher.tsx:145` | A 409 after capture has **no** retry affordance, by construction. This is the correct handling of money-captured-and-unfulfillable. The redesign adds motion around it, never a retry path into it. KEEP. |
| `formatINR` — display-only, no arithmetic | `lib/money.ts` | The comment is the design rule: display formatting, never money math. KEEP. |
| `metadataBase`, `%s — Maison Aurel` title template | `layout.tsx:22-27` | KEEP. |

### CHANGE — replaced, with before and after

| # | Asset | Before | After | Why |
|---|---|---|---|---|
| 1 | Focus ring colour | `outline: 2px solid #8a5a24` | `outline: 2px solid var(--focus) #9c6a2e` + `box-shadow: 0 0 0 4px var(--focus-halo)` | `#8a5a24` is **2.97:1 on `bg-ink`** (computed, §2.1) — under the 3:1 floor of 1.4.11 for a focus indicator on every dark button in the build. `#9c6a2e` is 3.76:1 on ink and 4.38:1 on paper, so one token works on both poles. The halo is the second guarantee. |
| 2 | Error / destructive colour | `text-bronze` (`FilterBar`, `CouponForm`, `CartLineItem`, `AddressForm`, `LoginForm`, `CheckoutView`, `PaymentLauncher`) | `text-danger #b42318` (6.19:1 on paper) | A form error shown in the brand accent is a semantic failure and it reads as decorative. Spec §15.1 names an Error token. Bronze stays the accent; bronze must never mean "this failed". |
| 3 | Success / in-stock colour | implicit — a 6px bronze dot | `text-success #1f6f47` (5.78:1) or a 6px dot in the same | Same reason. Spec §15.1 names Success. |
| 4 | Accent **on dark surfaces** | `text-bronze` would be 2.97:1 on `bg-ink`; only `text-bronze-soft` was used (`toast.tsx:88`) | new `--bronze-light #c9a26a` (7.38:1 on ink) | Makes the rule enforceable instead of accidental. A component reaching for an accent on ink now has a token that passes. |
| 5 | Reduced-motion enforcement | one blanket `animation-duration: .01ms !important` rule (`globals.css:117`) | the same rule **plus** every scroll-driven animation wrapped in `@media (prefers-reduced-motion: no-preference)` and every duration sourced from `--motion-*` tokens | The blanket rule is not sufficient once `animation-timeline: view()` exists: on a progress-based timeline the duration is the mapper, so forcing `0.01ms` lands the element on whatever `animation-fill-mode` happens to select. It is *probably* visible, but "probably" is not an accessibility guarantee. The `no-preference` gate makes it deterministic; the blanket rule stays as the backstop. This is the token-layer policy §3 requires. |
| 6 | `.card-lift` | transitions `transform` **and** `box-shadow` (`globals.css:93`) | `transform: translateY(-2px)` only; the elevation cue becomes a `border-color` change on hover | `box-shadow` is a **paint** property. Animating it per frame on 12 grid cards is exactly what §0 forbids. `border-color` on a 1px hairline is a far cheaper paint and, at this scale, indistinguishable. |
| 7 | `.skeleton` shimmer | animates `background-position` (`globals.css:47`) | a `::after` overlay translating `translateX(-100% → 200%)` over a flat `--line` base | Moves the shimmer from a per-frame paint of the whole box to a composited transform of one layer. Same 0 KB, one less paint. Capped at 12 instances per page (§3). |
| 8 | Product card hover | `group-hover:scale-[1.04]` on the image, 500ms (`ProductCard.tsx:26`) | `scale(1.03)` at 240ms on the image, and **nothing at all** on a card whose image failed to load | 500ms is above the `--motion-enter` ceiling of 220ms; it reads as sluggish rather than expensive. Also the monogram fallback currently has no hover affordance, so the card is inconsistent in its two states. |
| 9 | Scroll-driven work | none — `Header.tsx:54` uses a JS `scroll` listener for an 8px shadow threshold | CSS `animation-timeline` for narrative reveals; the header threshold **stays** a listener | See §3. The header listener is already correct (`{passive:true}`, and React bails out when the boolean is unchanged, so it re-renders once per crossing, not per frame). Replacing it would be motion for motion's sake. |
| 10 | Type scale | ad-hoc: `text-6xl`, `text-8xl`, `text-4xl`, `text-3xl`, `text-xl`, `text-lg`, plus raw `text-xs`/`text-sm` and `[0.2em]`/`[0.25em]` tracking values | a 9-step named scale (§2.2) + two tracking tokens | Nine heading sizes is a system that has lost its argument. The arbitrary tracking values are the same drift one layer down. |
| 11 | `/checkout` anonymous state | "Sign in to check out" wall (`CheckoutView.tsx:50`) | guest checkout, with "create an account for faster checkout" as an **offer**, not a gate | The backend is `AllowAny` and mints the guest token. See §5.4, §12-G1. |
| 12 | `/checkout` totals | cart subtotal only, "final totals are confirmed at checkout" | a delivery line from `GET /api/shipping/estimate/`, then the server total as the single authoritative figure | Spec §3.7 requires the delivery cost to be visible before payment. See §12-G3. |
| 13 | `/orders/[id]` data | fetch the full list, `.find()` | `GET /api/orders/{id}/` | §13 "server-side data fetching … where it benefits performance". One order, one request. |
| 14 | `types.ts` `Order` / `OrderItem` | missing 13 fields the backend sends | extended to the serializer | §12-G4. Stale types are how a lifecycle gets dropped on the floor. |
| 15 | Coupon | `sessionStorage` + stateless preview | `POST`/`DELETE /api/cart/coupon/` | A refresh currently loses the applied code. §12-G5. |
| 16 | PDP below the fold | description only (`products/[slug]/page.tsx:162`) | notes (from the description, no new data needed), restock opt-in for sold-out, delivery/returns line, and a **tracking link** for an order containing that product | Spec §3.4. The data exists; the page does not use it. |
| 17 | `robots.ts` disallow list | missing `/contact`, `/returns`, `/shipping`, `/staff`, `/staff/login`, `/staff/mfa/enroll` | list extended | `/staff/*` is a role-gated surface (spec §4.1) and is currently crawlable-but-unlinked. |
| 18 | `/admin` | a bare `redirect()` page | same, plus a `noindex` (already present) | KEEP the redirect — the Django admin is on the API origin and a local `/admin` is a 404 waiting to happen. |

### DROP — deleted, and what depended on it

| Asset | Where | What depended on it |
|---|---|---|
| Any announcement marquee / ticker | nowhere — **DROP as a prohibition, not a deletion** | Four of the six reference sites run a continuously scrolling promo bar (`uffco.com`, `fraganote.com`, `celestialperfume.in`, `perfumeoilcorner.com` all repeat one offer string 8–12 times). It is the single loudest thing in the category and it fails WCAG 2.2.2 Pause/Stop/Hide, which is AA. It is named in every negative list in `STITCH-PROMPTS.md`. |
| `text-ghost` on mobile | `page.tsx:69` (`hidden … lg:block` already) | nothing — already correctly hidden below `lg`. |
| The `.card-lift` box-shadow transition | `globals.css:95` | nothing else. |
| `PLAN.md` §4's "mobile PDP gets a sticky bottom add-to-cart bar" as an *unbuilt* idea | `PLAN.md` | It is **built** into this spec, not dropped — see §5.2. Listed here only to record that PLAN.md's "no swipeable gallery" decision is now also a backend fact (single `ImageField`). |

### The recorded AA status, confirmed in source

| Claim | Verified? | Evidence |
|---|---|---|
| Palette and computed text-pair contrast pass AA | **Yes**, with one boundary | Every pair in §2.1 recomputed from the hex values. Boundary: `line` at 1.23:1 on paper is decorative only; WCAG 1.4.11 does not apply to it and it must never become a control boundary. The focus ring on `bg-ink` was **failing** at 2.97:1 — CHANGE-1 fixes a real pre-existing gap, it does not introduce one. |
| An interactive-state matrix exists | **Partially** | Hover / focus-visible / disabled are present on every control I read. **Absent**: the spec §15.3 required list — per-component *loading*, *error*, *success* and *mobile-behaviour* rows. Loading/empty/error exist ad hoc inside each view (`CartView`, `CheckoutView`, `OrdersListView`, `OrderDetailView`) rather than as a shared matrix. §6 rebuilds it. |
| A focus trap exists | **Yes** | `MenuOverlay.tsx:26-64`. It is the only dialog in the app; Razorpay's modal is third-party and out of our DOM. |
| A skip link exists | **Yes** | `layout.tsx:52`. |
| `prefers-reduced-motion` is handled | **Yes, but insufficiently** | `globals.css:117`. Correct for keyframe and transition work. Not deterministic for scroll-driven animations — CHANGE-5. |

---

## 1. Design principles

Five. Each has a concrete example, and each has the condition under which it is
**wrong** — a principle without a falsifier is a mood board.

### P1 — Whitespace is the product. Hierarchy is the whole design.

Not "use generous spacing". Specifically: the gap between two sections is
`--space-32` (128px) at desktop and `--space-16` (64px) at phone, and the gap
between two elements *inside* a section is never more than `--space-8` (32px).
The ratio between those two numbers — 4:1 — is the layout signature of the site.

Evidence: every one of the six reference sites fills its viewport. `uffco.com`
runs a promo marquee above a nav with fourteen entries and a "Popular Searches"
block; `fraganote.com` stacks four nav trees, two search modules and a
trending-search row above the first product; `bellavitaorganic.com` repeats its
entire category tree twice on one screen. `celestialperfume.in` puts five nav
columns above a hero. None of these sites is expensive-looking. They are
*busy*-looking, and they read as cheap.

*Wrong when:* the page is `/checkout` step 2, where the customer's task is to
compare three line items. There, the correct density is high and the whitespace
is one `--space-12` between the steps. Whitespace is not a moral virtue; it is
what you spend when nothing needs to be compared.

### P2 — One accent moment per page. Everything else is still.

A page may have at most one moment that moves on its own. Everything else moves
only in response to a state change the customer caused.

Evidence: the reference sites animate *everything* — marquees, hover zooms,
badge pulses, drawer slides, and (on `celestialperfume.in` and
`perfumeoilcorner.com`) a slide-in cart drawer that appears over the content on
load. That is what "no clear focal point" looks like in motion.

The accent moment per page is named in `STITCH-PROMPTS.md` §2. It is not
negotiable per page without a reason recorded here.

*Wrong when:* the page has no image and no narrative to sequence — the legal
pages, the recovery pages. Those have **zero** accent moments, not a small one.
A quiet page that is 2% animated is noisier than one that is 0%.

### P3 — Motion is a sentence, not an adverb.

**The decision test.** An animation ships only if it answers *which* of these two
questions, out loud:

1. **What state is this in?** (communicate state) — the toast confirms an add, the
   cart badge morph shows *where* the item went, the order-tracking marker fills
   as the trail is read, the checkout progress hairline shows which step.
2. **What should I read next, in what order?** (direct attention through
   narrative) — the PDP story image drifting as the notes section is read; the
   tracking timeline revealing parcels oldest-first.

If it answers neither, it is cut. "It looks nice", "it feels premium", "it adds
polish" are all the same answer: **cut**.

Two sub-rules that make the test checkable:

- **No animation may gate or delay an interaction.** If a customer's click
  depends on an animation finishing, the animation is wrong. This is why nothing
  above the fold animates in (§10), and why the checkout submit button never
  animates.
- **No animation may carry information that is not also present as text.** The
  tracking timeline fills markers; it also prints the latest status as text at
  the top of the page. A customer with reduced motion, a crawler, or a screen
  reader loses nothing.

*Wrong when:* applied to a hover affordance. Hover micro-motion answers neither
question — but it is retained at 240ms and 1.03 scale as *feedback that the
target is live*, which is a state answer (question 1) at the smallest possible
scale. Beyond 240ms or 1.04 scale it becomes decoration and is cut.

### P4 — The single image is the page; the type frames it.

The product model has exactly one `ImageField`. So the image is treated as a
photograph with a mat around it: an explicit aspect box, one background tone, no
crop tricks, no collage, no thumbnail strip pretending to be a gallery.

Evidence: `perfumeoilcorner.com` runs a genuine 30/50/100ml **variant selector**
on the card, because it has three SKUs per fragrance. Our `ProductSerializer` has
one row with one `size` and one `price`. A size selector on our PDP would be
fabricating a choice the data cannot honour — so §5.2 specifies a **static size
line**, not a selector.

*Wrong when:* the brand later ships `Product.images` (already logged as P2 in
`BACKEND_REQUESTS.md`). At that point §5.2 gets a real gallery and this principle
is superseded, not extended.

### P5 — Say the real number, or say nothing.

Every figure on screen is either a server response rendered verbatim, or a
figure the customer themselves produced. There is no third category.

This is not only a design rule, it is the backend's rule (§4.5, §13 "Avoid
duplicating price calculations in multiple components"), and the two agree
because both were written from the same source. It has three visible
consequences:

- The cart's subtotal is labelled an **estimate** and says so.
- The checkout delivery line comes from `GET /api/shipping/estimate/` or it is
  not shown; it is never computed on the client.
- **No animated number counters** (§3, R6). A rolling figure is unreadable at the
  moment it is read, and it implies intermediate values that never existed.

*Wrong when:* the label would be longer than the number. "₹1" and
"₹1 (server-estimated; final amount confirmed by the payment gateway)" — the
first is the truth, the second is noise. Put the caveat in the surrounding
sentence, not in the figure.

### What the reference sites actually taught us

They are not a style to copy; they are a **noise floor to stay well under**. The
useful findings were all negative, and each one is now a prohibition in
`STITCH-PROMPTS.md`:

| Observed on | Finding | Consequence here |
|---|---|---|
| all four Shopify stores | an infinite announcement marquee | R10 in §3 — fails WCAG 2.2.2 (AA) |
| `celestialperfume.in`, `uffco.com` | product names carry celebrity attributions ("Worn by Alia Bh@tt") and "Inspired By" disclosures on every card | we ship **no** attributions and no clone framing; the house is a house |
| `fraganote.com`, `bellavitaorganic.com`, `perfumeoilcorner.com` | star ratings with review counts on every card (207, 1558, 177…) | no star ratings anywhere. There is **no reviews endpoint**. Spec §6.11 exists on the backend for moderation, not for a customer surface we can read. |
| `bellavitaorganic.com` | "48% OFF", "51% OFF", "26% OFF" on every single card, plus a cashback app and "gifts under ₹599" navigation | no percentage-off badges. `Product` has no `compare_at_price`; a struck-through price would be invented. |
| `perfumeoilcorner.com` | a lookalike-domain warning modal, a "3 Lakhs+ Happy customers" claim, and an award badge | no trust badges, no customer counts, no award claims. Spec §15 has none of these and none can be sourced. |
| all four | a slide-in cart drawer that overlays content | kept out: `PLAN.md` §4 already decided a well-built `/cart` page beats two half-built surfaces. One surface per job. |
| `celestialperfume.in` | "Buy 2 Get 2 Free" on every single product card | no per-product offer badges. |

---

## 2. Tokens

The single source of truth. Everything else cites these names.

### 2.1 Colour

Every text/background pair that ships states its **computed contrast ratio** and
its target (AA normal text 4.5:1, AA large text 3:1, non-text/UI 3:1).

Ratios below are **computed**, not estimated, using the WCAG 2.x relative
luminance formula (sRGB → linearise → `0.2126R + 0.7152G + 0.0722B` →
`(L1+0.05)/(L2+0.05)`) on the exact hex values in `globals.css:5-15`. Any token
whose value changes invalidates its row and must be recomputed.

| Token | Value | On | Ratio | Target | Meets | Where it is used |
|---|---|---|---|---|---|---|
| `--color-ink` | `#1c1917` | `--color-paper` | **16.48:1** | 4.5 (normal) | ✅ | body text, headings, buttons on `paper` |
| `--color-ink` | `#1c1917` | `--color-surface` | **17.49:1** | 4.5 | ✅ | form fields, cards, panels |
| `--color-ink` | `#1c1917` | `--color-bronze-soft` | **14.51:1** | 4.5 | ✅ | monogram fallback, price on a tinted plate |
| `--color-paper` | `#faf8f3` | `--color-ink` | **16.48:1** | 4.5 | ✅ | solid-ink buttons, badges, toast body |
| `--color-ink-muted` | `#57534e` | `--color-paper` | **7.19:1** | 4.5 | ✅ | secondary copy, placeholders, footer legal |
| `--color-ink-muted` | `#57534e` | `--color-surface` | **7.63:1** | 4.5 | ✅ | field placeholders — **the `ui.test.ts` pin** |
| `--color-ink-muted` | `#57534e` | `--color-bronze-soft` | **6.33:1** | 4.5 | ✅ | muted text inside a tinted panel |
| `--color-bronze` | `#8a5a24` | `--color-paper` | **5.54:1** | 4.5 | ✅ | eyebrows, hover text, focus-adjacent accents |
| `--color-bronze` | `#8a5a24` | `--color-surface` | **5.88:1** | 4.5 | ✅ | eyebrow labels, "sold out" chip text |
| `--color-bronze` | `#8a5a24` | `--color-bronze-soft` | **4.88:1** | 4.5 | ✅ | monogram glyph (`bronze/60` → see exemption E3) |
| `--color-line-strong` | `#75706a` | `--color-surface` | **4.90:1** | 3 (1.4.11) | ✅ | **field borders** — the `ui.test.ts` pin |
| `--color-line-strong` | `#75706a` | `--color-paper` | **4.62:1** | 3 | ✅ | field borders on `paper` |
| `--color-line-strong` | `#75706a` | `--color-bronze-soft` | **4.07:1** | 3 | ✅ | field borders on a tinted plate |
| `--color-line` | `#e6e1d7` | `--color-paper` | **1.23:1** | — | **decorative only** | section rules, card borders, list dividers. Never a control boundary. |
| `--color-line` | `#e6e1d7` | `--color-surface` | **1.30:1** | — | **decorative only** | as above |
| `--color-bronze-soft` | `#f1e9dc` | `--color-surface` | **1.20:1** | — | **decorative only** | image placeholder plate, `:selection`, cart-count fill |
| `--color-bronze-soft` | `#f1e9dc` | `--color-paper` | **1.14:1** | — | **decorative only** | as above |

**New tokens.** Four, all additive. None replaces an existing value.

| Token | Value | On | Ratio | Target | Meets | Purpose |
|---|---|---|---|---|---|---|
| `--color-focus` | `#9c6a2e` | `--color-paper` | **4.38:1** | 3 (1.4.11) | ✅ | focus ring, light surfaces |
| `--color-focus` | `#9c6a2e` | `--color-surface` | **4.65:1** | 3 | ✅ | focus ring, fields and cards |
| `--color-focus` | `#9c6a2e` | `--color-ink` | **3.76:1** | 3 | ✅ | focus ring **on solid-ink buttons** — the case `#8a5a24` failed at 2.97:1 |
| `--color-focus` | `#9c6a2e` | `--color-bronze-soft` | **3.86:1** | 3 | ✅ | focus ring on a tinted plate |
| `--color-bronze-light` | `#c9a26a` | `--color-ink` | **7.38:1** | 4.5 | ✅ | the only accent permitted on a dark surface |
| `--color-danger` | `#b42318` | `--color-paper` | **6.19:1** | 4.5 | ✅ | field errors, `role="alert"`, capture-failure border |
| `--color-danger` | `#b42318` | `--color-surface` | **6.57:1** | 4.5 | ✅ | as above |
| `--color-danger` | `#b42318` | `--color-bronze-soft` | **5.46:1** | 4.5 | ✅ | error inside a tinted panel |
| `--color-success` | `#1f6f47` | `--color-paper` | **5.78:1** | 4.5 | ✅ | "In stock", applied-coupon confirmation |
| `--color-success` | `#1f6f47` | `--color-surface` | **6.13:1** | 4.5 | ✅ | as above |
| `--color-success` | `#1f6f47` | `--color-bronze-soft` | **5.09:1** | 4.5 | ✅ | as above |

Spec §15.1 offers `#B42318` (Error) and `#247A50` (Success). `#247A50` measures
**4.98:1** on paper — it passes, but with 0.48 of headroom, and it is the tighter
of the two. `#1f6f47` at 5.78:1 is the same hue family with real margin. Adopted
on that basis; the spec's own note is that the palette there is "illustrative".

**Exemptions, stated so they cannot be quietly widened.** WCAG 1.4.3 excludes
purely decorative content. Three elements rely on that and each carries a
condition that keeps it honest:

- **E1 — `.text-ghost` watermark** (`line` stroke, 1.23:1). Legal only because it
  is `aria-hidden`, `pointer-events-none`, carries no information, and is
  `hidden` below `lg`. **If it ever becomes readable text, it must move to
  `--color-ink-muted`.**
- **E2 — `--color-line` hairlines** (1.23:1). Legal as a *decorative* divider. It
  is **not** legal as the sole indicator of a control's boundary — that is why
  fields use `--color-line-strong` (4.62–4.90:1). Any new control must use
  `--color-line-strong`.
- **E3 — `text-bronze/60` monogram** (`ProductImage.tsx:38`). Full-strength
  `bronze` on `bronze-soft` is 4.88:1; the `/60` composited variant lands near
  2.1:1. Legal only because the whole block is `aria-hidden="true"` and the
  product name is in the `alt`/`aria-label`. **Do not apply `/NN` opacity to any
  text that is not already decorative** — that is precisely the bug
  `ui.test.ts` pins against for placeholders.

**The rule.** A new colour is added only with (a) a computed ratio recorded
against every background it will appear on, and (b) a named role. There is no
"temporary" colour in this system; if a role is missing, the token is missing,
not improvised.

### 2.2 Typography

**The pairing — KEEP, unchanged.** `Fraunces` (variable, display: headings,
product names, prices-as-display, the wordmark) + `Source Sans 3` (variable,
text: body, UI, labels, all form controls). Already wired at `layout.tsx:9-19`.

Why this pairing and not a swap: Fraunces is a *soft-serif with wonk*. Its
high-contrast didone-adjacent strokes and its slightly irregular terminals read
as hand-set rather than machine-set, which is the whole luxury register. It also
holds up at `clamp()`-sized hero sizes because its optical-size axis means the
counters do not go spindly. Source Sans 3 is a humanist sans with a large x-height
and unambiguous `l`/`1`/`I` — it is legible at 12px in a filter field, which is
where a storefront actually loses money (a mis-read price filter).

**What is added: the optical axis, for free.** `Fraunces` is a variable font
carrying `wght` (100–900), `opsz` (9–144), `SOFT` (0–100) and `WONK` (0–1). The
current build loads the variable file and uses none of that. Setting
`font-variation-settings` per size step costs **0 KB** — the axes are already in
the same `woff2`. The rule: `opsz` tracks the rendered size, `WONK` and `SOFT` are
pinned to 0 everywhere except the wordmark.

**The scale — 9 steps, replacing 9 ad-hoc Tailwind sizes.** Named, not
arbitrary. `clamp()` on the three fluid steps so the hero does not need a
breakpoint override.

| Token | Size | Line-height | Tracking | Family | Where |
|---|---|---|---|---|---|
| `--text-display` | `clamp(2.75rem, 1.6rem + 5.2vw, 5rem)` | `0.98` | `-0.02em` | Fraunces 500 | home hero only |
| `--text-h1` | `clamp(2rem, 1.5rem + 2.2vw, 3rem)` | `1.04` | `-0.015em` | Fraunces 500 | page titles, PDP name |
| `--text-h2` | `clamp(1.5rem, 1.25rem + 1.1vw, 2rem)` | `1.1` | `-0.01em` | Fraunces 500 | section headings |
| `--text-h3` | `1.25rem` | `1.2` | `-0.005em` | Fraunces 500 | card titles, subsection heads |
| `--text-body-lg` | `1.125rem` | `1.6` | `0` | Source Sans 3 400 | PDP description, editorial paragraphs |
| `--text-body` | `1rem` | `1.6` | `0` | Source Sans 3 400 | default body, cart lines |
| `--text-small` | `0.875rem` | `1.5` | `0` | Source Sans 3 400 | metadata, secondary copy, fields |
| `--text-label` | `0.75rem` | `1.4` | `0.15em` + uppercase | Source Sans 3 600 | eyebrows, section labels, table headers |
| `--text-money` | `1rem` (tabular) | `1.4` | `0` | Source Sans 3 600 + `font-variant-numeric: tabular-nums` | every price, total, discount |

The `600` label weight with `0.15em` tracking replaces the four different
arbitrary tracking values currently in the tree (`[0.15em]`, `[0.2em]`,
`[0.25em]`, and none).

**Line-height rules, stated as rules:**

- Display and heading steps are set **tight** (0.98–1.2). Fraunces has a large
  internal leading; loose leading on a serif display makes it look like a
  document, not a storefront.
- Body steps are set at **1.6**. Below 1.5 a 16px sans paragraph in `ink-muted`
  at 7.19:1 loses its shape on a phone at arm's length; above 1.7 it stops being
  a paragraph and starts being a layout.
- **No line length above 68ch.** `--measure` caps prose at `34rem`. The PDP
  description, the house-position copy and every legal section use it. A luxury
  storefront with 90-character measure reads as a blog, not a label.
- Money is **tabular**. `formatINR` already returns a grouped string; without
  `tabular-nums` a subtotal re-renders on every quantity change and the digits
  jitter, which reads as a *different* number.

**Loading strategy.** Self-hosted by `next/font/google` at build time; the Google
Fonts download happens during `next build`, and only local `.woff2` files are
ever served. Consequences, all verified in config:

- `font-src 'self'` in the CSP (`next.config.ts:24`) is therefore already
  correct and needs no third-party origin. **Do not add a `preconnect` to
  `fonts.gstatic.com`** — there is no request to preconnect to. That is a common
  and entirely wasted optimisation for a `next/font` build.
- `subsets: ["latin"]`, `display: "swap"`.
- **Both families are variable, so both are one file each regardless of how many
  weights are used.** Asking for `weight: ["400","600"]` on a non-variable request
  would pull *two* files per family and roughly double the font bytes for no
  benefit. Keep them variable.
- **The `swap` trade-off.** `swap` means a period of fallback-text rendering.
  `next/font` generates a metric-adjusted fallback face (`size-adjust`,
  `ascent-override`, `descent-override`, `line-gap-override`) so the fallback
  occupies the same box as the webfont — which is what keeps CLS at ~0 despite
  `swap`. **Verify before claiming it:** open the built CSS and confirm the
  generated `@font-face` for the fallback carries those descriptors. If it does
  not, this is a CLS regression and the fix is `adjustFontFallback` explicitly
  set, not `display: "optional"`.
- Both fonts are used in `layout.tsx`, so `next/font` preloads both on every
  route. At an estimated **35–55 KB gz each** for a latin-subset variable file
  that is roughly 90–110 KB of font. That is a real number against a 120 KB JS
  budget (fonts are a separate budget — §10) and the honest mitigation is
  **not** a second font. If it ever needs cutting, the axis-range-subset route is
  unavailable through `next/font/google`; the correct lever is `opsz` clamping in
  CSS, which does not reduce bytes. Recorded as a known cost, not a solved problem.

*Wrong when:* a page needs a third family. This site will not get one. A
monospace for order numbers is the only candidate and it is refused: the order
reference is read aloud on WhatsApp support calls, where a human reads it, and a
third webfont would cost more than the legibility is worth.

### 2.3 Spacing, radii, borders, elevation, z-index

**Spacing — a 10-step scale, extending spec §15.1's `4,8,12,16,24,32,48,64`.**
The two added steps (`96`, `128`) are the editorial ones: the hero and the
between-section rhythm need a value and `64` was being stretched to do it, which
is what makes a scale decay.

| Token | px | Role |
|---|---|---|
| `--space-1` | 4 | icon-to-label inside a control |
| `--space-2` | 8 | tight label pairs, badge padding |
| `--space-3` | 12 | inside a control, list row padding |
| `--space-4` | 16 | **default gap between siblings** |
| `--space-6` | 24 | between a heading and its first child |
| `--space-8` | 32 | card internal padding, sidebar gutters |
| `--space-12` | 48 | between blocks *inside* a section |
| `--space-16` | 64 | **between sections, phone** |
| `--space-24` | 96 | between sections, tablet |
| `--space-32` | 128 | **between sections, desktop**; hero vertical padding |

**The rule that makes a value land on the scale rather than near it.** There is
no "close enough" and no arbitrary value; Tailwind arbitrary spacing
(`p-[13px]`, `mt-[18px]`) is banned outright and is the one rule here that a
reviewer can enforce with a single grep. The *semantic* rule behind it:

> **If you cannot name which two things you are separating, you are spacing
> things that are not siblings — fix the structure instead of the gap.**

Concretely: the gap between two elements is chosen from **sibling relationships**
(`--space-4` default, `--space-6` after a heading, `--space-12` between blocks).
The gap between two *sections* comes from the responsive rhythm above and is
never chosen per section. This is why P1 could state a 4:1 ratio as the layout
signature: the ratio is a consequence of the rule, not a separate convention.

**Radii — the corner language is square. `0px`, everywhere.**

This is the load-bearing part of the current build and it is KEEP. There is
exactly one exception, and it is a number, not a container: the cart-count
counter in the header, which is `rounded-full` because a count is a lozenge. A
rounded *container* is a different object and this system has none — no cards, no
buttons, no inputs, no panels are rounded. Compare the reference sites, where
every surface is an 8–24px pill or radius and the page reads as a template
because of it.

**Borders — 1px only, two tokens, by role.**

| Token | Value | Role | Ratio (surface) |
|---|---|---|---|
| `--border-hairline` | `1px solid var(--color-line)` | section rules, card edges, list dividers, table rows | 1.30:1 — decorative (E2) |
| `--border-field` | `1px solid var(--color-line-strong)` | **every** input, select, textarea, and any control whose boundary is the only affordance | 4.90:1 — passes 1.4.11 |
| `--border-strong` | `1px solid var(--color-ink)` | the secondary/tertiary button (`Apply`, `Back`) and the current pagination page | 17.49:1 |
| `--border-danger` | `1px solid var(--color-danger)` | the `captured-dead-end` panel | 6.57:1 |

No 2px borders except the focus ring's `2px` outline. No `outline` for anything
but focus.

**Elevation — two shadows exist in this entire design system, and neither
animates a shadow.**

| Token | Value | Role |
|---|---|---|
| `--elevation-bar` | `0 2px 12px -6px rgb(28 25 23 / 0.15)` | the header, once the page has scrolled under it (`Header.tsx:65` today) |
| `--elevation-modal` | `0 24px 48px -24px rgb(28 25 23 / 0.28)` | the mobile menu overlay only, so the page beneath reads as *dismissed*, not as *scrolled* |

That is the complete list. Card hover is **not** elevation — per CHANGE-6 it is a
`translateY(-2px)` plus a `border-color` change. There is no "card shadow", no
"dropdown shadow", no "sticky bar shadow". Every other surface in the system is
separated by a hairline. This is the cheapest possible elevation strategy
(hairlines composite; shadows do not) and it is also the correct luxury one: a
shadow says *floating*, and nothing on this site floats.

**Z-index — an explicit scale, 5 steps, no exceptions.** The current build has
three `z-50`s (skip link, menu overlay, toast container) that are ordered
correctly only by DOM accident. That is a latent bug and it is replaced by a
scale.

| Token | z | Element | Why here |
|---|---|---|---|
| `--z-sticky` | 30 | sticky sidebar purchase/summary panels, sticky mobile purchase bar | below the bar, above content |
| `--z-header` | 40 | `<header>` | current value; correct |
| `--z-overlay` | 50 | menu overlay, any modal | above the header it covers |
| `--z-toast` | 60 | toast container | **above the overlay** — an add-to-cart confirmation must not be hidden behind the menu the customer just opened |
| `--z-skip` | 70 | skip link | must be visible over everything, including the overlay |

Rule: a new z-index is a new token, or it is a bug. No inline `z-[55]`.

### 2.4 Motion tokens

Durations and easings **by intent class**, not by component — so the whole site
feels like one system.

Defined in `@theme` in `globals.css` alongside the colour tokens, so a component
never writes a duration in milliseconds.

| Intent | Token | Duration | Easing | Reduced-motion behaviour |
|---|---|---|---|---|
| `state` | `--motion-state-dur` | **120ms** | `cubic-bezier(0.2, 0, 0, 1)` | instant. Colour and border changes need no motion at all; they simply take 0ms. |
| `enter` | `--motion-enter-dur` | **220ms** | `cubic-bezier(0.16, 1, 0.3, 1)` | instant. The element appears at its final position. **No opacity flash** — a 0ms opacity transition is a flicker, not an absence. |
| `exit` | `--motion-exit-dur` | **160ms** | `cubic-bezier(0.4, 0, 1, 1)` | instant. The element is removed. |
| `hover` | `--motion-hover-dur` | **240ms** | `cubic-bezier(0.2, 0, 0, 1)` | instant. |
| `narrative` | `--motion-narrative-dur` | **600ms** | `cubic-bezier(0.16, 1, 0.3, 1)` | instant. Used only by the one accent moment per page. |
| `progress` | `--motion-progress-dur` | **180ms** | `cubic-bezier(0.2, 0, 0, 1)` | instant. The progress hairline snaps to its final position. |
| `scrub` | — (timeline-driven) | n/a | `linear` | **the animation is removed entirely**, not shortened (§3) |
| `loading` | `--motion-loading-dur` | **1200ms** | `linear`, infinite | **static**. The skeleton becomes a flat `--color-line` block. No shimmer at all — a shimmer is the one thing that is pure "something is happening" with no information in it. |
| `toast` | `--motion-toast-dur` | **200ms** | `cubic-bezier(0.16, 1, 0.3, 1)` | **opacity only, 100ms.** A toast is an announcement; the announcement must survive, only the movement is dropped. |

The three names that matter:

- `enter` (220ms) and `exit` (160ms) are deliberately **asymmetric and the exit
  is faster**. Symmetric timing is the default that makes an interface feel
  mechanical; a shorter exit reads as "that is done".
- `narrative` (600ms) is the only duration above 240ms. It exists so the accent
  moment can be *slow* enough to be noticed once. It appears in exactly one
  place per page.
- Nothing is `linear` except the shimmer and the scrub timelines. `linear` on a
  UI transition is the reason things feel cheap.

**Easing, defined as curves with a reason:**

| Curve | Shape | Why it exists |
|---|---|---|
| `cubic-bezier(0.2, 0, 0, 1)` | fast start, long settle | state and hover. The element is *already* moving when the eye catches it; a long settle is what makes it read as responsive rather than sluggish. |
| `cubic-bezier(0.16, 1, 0.3, 1)` | near-instant start, very long tail | enter and narrative. This is an ease-out-expo; it front-loads the movement so the perceptual latency is ~1 frame even though the tail is 600ms. |
| `cubic-bezier(0.4, 0, 1, 1)` | slow start, fast exit | exit. It acknowledges that the element is leaving before committing. |
| `linear` | — | only where the input is itself a continuous timeline (scrub) or a repeating loop (shimmer). |

**The `hover` 1.03 scale is a token too:** `--hover-scale: 1.03`. Capped. The
current `1.04` at 500ms (`ProductCard.tsx:26`) is the only value that has to
change, and it changes because 500ms is over the `hover` ceiling.

**Why durations live in tokens and not per component.** A component that writes
`220ms` is correct today and inconsistent after the next redesign. A component
that writes `var(--motion-enter-dur)` is correct forever. This is also what makes
the reduced-motion override a one-line change in one place (§3), which is the
whole point of "honoured at the token layer".

---

## 3. Motion

The load-bearing table. The "why" column is not commentary — it is the
acceptance criterion. A row whose "why" cannot be phrased as *communicates
state* or *directs attention* does not ship (P3).

Every "Cost" cell states: the mechanism, the bytes, whether it runs on the
compositor, whether it needs JS per frame, and whether it can block LCP.

| Element | Trigger | Technique | Duration | Easing | Reduced-motion | Why it earns its place | Cost |
|---|---|---|---|---|---|---|---|
| **Home — hero eyebrow + headline + sub** | page load | CSS `@keyframes` on `opacity` + `translateY(12px)`, `animation-fill-mode: both` | `--motion-enter` 220ms | `cubic-bezier(0.16, 1, 0.3, 1)` | instant, no fade | Communicates *arrival*. It is the only entrance animation on the site, and it fires once. | CSS-only, 0 KB. `transform`+`opacity` → composited, no layout, no paint. 12ms of main-thread style recalc. **Does not block LCP**: the headline is not the LCP element (the bottle image is) and the animation starts at `opacity: 0.01` not `0`, so the text still registers for LCP. Never delay it on `LCP`. |
| **Home — New-arrivals grid, house-position block** | scrolled into view | **`animation-timeline: view()`** on `opacity` + `translateY(16px)`, `animation-range: entry 0% entry 35%`, wrapped in `@media (prefers-reduced-motion: no-preference)` | scrub (timeline) | `linear` | **animation absent** | Directs attention down the page in the reading order. Two sections, not ten — a card grid that staggers reads as decoration. | CSS-only, 0 KB. No scroll listener, no JS frame. `transform`+`opacity` composited. **Not supported on Firefox today** → the `@supports` default state must be `opacity:1`, so those browsers and all crawlers see the final state. **Never on the hero.** |
| **PDP — purchase column + breadcrumb** | page load | CSS keyframe, same curve as the home hero | 220ms | `cubic-bezier(0.16, 1, 0.3, 1)` | instant | Communicates that the right-hand column is the actionable one. It is the *second* thing a customer reads and the first thing they act on. | CSS-only, 0 KB, composited. |
| **PDP — bottle image** | page load | **NO ANIMATION.** Painted once. | — | — | — | — | This is a deliberate **correction to the worked example in `STITCH-PROMPTS.md` §3.2**, which specifies a "slow scale-and-settle on the gallery image as it enters". That image is the LCP element on every PDP. A `scale` on an LCP element changes its painted size over 600ms, which (a) risks registering the LCP timestamp at animation end rather than first paint, and (b) makes a 4:5 photograph visibly resize on the most important page. The motion moves to the purchase column instead. |
| **PDP — story/notes image** | scrolled past | `position: sticky` frame inside a `140vh` wrapper, `translateY` driven by `animation-timeline: scroll(root block)`, `animation-range` limited to the wrapper | scrub | `linear` | **animation absent** | Directs attention: the image holds while the notes beside it are read, then releases. Narrative, not decoration. | CSS-only, 0 KB, composited. **Cost is memory, not CPU**: a full-viewport sticky image stays as a composited layer for the whole 140vh span — on a low-end Android that is a real cost. Capped at 6% of viewport height of travel. **Phone only: disabled.** |
| **PDP / PLP — sold-out → "Notify me" confirmation** | state change | SVG `stroke-dashoffset` draw on a 16px check | `--motion-state` 120ms | `cubic-bezier(0.2, 0, 0, 1)` | instant, check appears whole | Communicates the opt-in succeeded. The only icon animation in the system. | CSS-only, 0 KB. `stroke-dashoffset` is a **paint** property, not composited — accepted because it is one 16px SVG, once, and it is not in any list. **Never on >8 items.** |
| **Add-to-cart — buy button → header cart badge** | `POST /api/cart/` resolves 200 | **View Transitions**: `view-transition-name` on the button and on the badge, `document.startViewTransition()` around the state flip | browser default (~250ms), snapped by the `exit`/`enter` tokens | browser + our curve | instant swap, no morph | Communicates *where the item went*. This is the clearest state answer on the site: the thing the customer clicked becomes the number in the header. | ~0.3 KB of JS (a 12-line wrapper). The browser snapshots two small elements — **two**, not the grid. GPU texture copies are the cost. **Rule:** the badge is inside a header that re-renders on a 15s cart poll (`Header.tsx:40-45`); the poll must be suspended while a transition is in flight or the snapshot is taken mid-update and the morph is cancelled. |
| **Cart — line removed** | `DELETE /api/cart/{id}/` resolves | **View Transitions** on the remaining lines, so the browser re-lays-out and cross-fades. **No hand-written `height` animation.** | browser default | browser | instant reflow, no cross-fade | Communicates that a line is gone and the rest have closed up. | 0.3 KB + one snapshot per visible line. **Cap: not used above 20 lines** — snapshot memory scales with the captured area. **Not used inside a scrollable sub-container** (snapshots are viewport-relative). This is the row that replaces `PLAN.md`'s proposed "CSS height/opacity transition", which would have run layout on every frame. |
| **Cart — quantity stepper value** | `PATCH` resolves | `opacity` + `translateY(4px)` on the number only | `--motion-state` 120ms | `cubic-bezier(0.2, 0, 0, 1)` | instant | Communicates that the *number changed*, which is otherwise a silent re-render — the customer changed something and nothing acknowledged it. | CSS-only, 0 KB, composited. Deliberately the number and not the row: moving the row would move the Remove button under the cursor. |
| **Cart / checkout — "Final totals are confirmed at checkout" note** | on success | none | — | — | — | It is static text, and the animation-adjacent decision is to **not** animate it. | 0 KB. |
| **Menu overlay — open** | `aria-expanded` flips | **`@starting-style` + `transition-behavior: allow-discrete`** on `opacity` + `translateY(-8px)`; `display` in the transition list | `--motion-enter` 220ms | `cubic-bezier(0.16, 1, 0.3, 1)` | instant, no fade | Communicates *the menu replaced the page*. The focus trap (KEEP) is the load-bearing part; the motion is a 200ms courtesy so the trap does not feel like a jump cut. | CSS-only, 0 KB, composited. `allow-discrete` is what lets `display:none` participate in the transition so the overlay can stay mounted for its exit — **without it there is no exit animation at all**. Chrome 117+, Safari 17.5+, Firefox 129+. Where unsupported the overlay still appears and still traps focus; only the fade is lost. |
| **Menu overlay — close** | Escape / close / route change | same, reversed | `--motion-exit` 160ms | `cubic-bezier(0.4, 0, 1, 1)` | instant | as above | as above |
| **Toast — enter / exit** | mutation resolves | KEEP the existing `@keyframes toast-in` (`globals.css:32`) | `--motion-toast` 200ms | `cubic-bezier(0.16, 1, 0.3, 1)` | **opacity only, 100ms** | Communicates that a write succeeded or failed, and the container is `aria-live="polite"` so the announcement is simultaneous. | CSS-only, 0 KB, composited. **The exit is currently absent** (the item is filtered out of the array after 4s with no exit). Add the `@starting-style`-equivalent exit; do not add a spring. |
| **Header — scroll shadow** | `scrollY > 8` | **KEEP the existing JS listener** (`Header.tsx:53-59`) | instant (threshold) | — | n/a | Communicates that the page has moved under a fixed element — the classic justification, and the reason the bar stops floating. | `{passive: true}` is already set; `setScrolled(boolean)` re-renders **once per crossing**, not per frame, because React bails on an unchanged value. 0 KB. Replacing this with `animation-timeline` would be motion for motion's sake. The one `box-shadow` paint per crossing is not a budget item. |
| **Checkout — step progress hairline** | step changes | `transform: scaleX()` on a 1px `--color-ink` rule | `--motion-progress` 180ms | `cubic-bezier(0.2, 0, 0, 1)` | instant | Communicates *which step you are on and how many remain*. | CSS-only, 0 KB, composited. **The panel content itself does not animate** — see the rejected list, R13. |
| **Checkout — step panel content** | step changes | **none** | — | — | — | It is a discrete state swap. | 0 KB. Animating it would imply the steps are spatially continuous, which they are not, and it would move the submit button. |
| **Payment — phase panels** (`working` → `dismissed` / `verify-error` / `captured-dead-end`) | gateway callback | **none.** Instant swap, `role="alert"` / `role="status"` unchanged | — | — | — | These are money states. The customer's next action must be available the instant the state changes; a transition on the element carrying the retry affordance is a transition on the wrong thing. | 0 KB. |
| **Orders — list row** | hover / focus | `border-color` → `--color-ink`, no transform | `--motion-hover` 240ms | `cubic-bezier(0.2, 0, 0, 1)` | instant | Live-target feedback (P3). | Composited-safe (border colour is a paint on a 1px box; one row at a time). |
| **Track order — timeline marker fill** | each event row scrolled into view | `animation-timeline: view()` per marker, `opacity` + `scale(0.9 → 1)`, `animation-range: entry 0% cover 22%` | scrub | `linear` | **markers rendered already filled** | Directs attention through the narrative of a parcel's journey, oldest parcel first. It is the one place where scroll position *means* something. | CSS-only, 0 KB, composited. The **latest status is also printed as text** at the top of the page, so the information never depends on the motion. Bounded by the number of shipments (typically 1–3). |
| **Skeletons** | loading | **`::after` overlay `translateX(-100% → 200%)`** over a flat `--color-line` base (CHANGE-7) | `--motion-loading` 1200ms | `linear`, infinite | **static flat block** | Communicates *this content is arriving*, which is real information: it distinguishes "loading" from "empty". | CSS-only, 0 KB, now composited (was a `background-position` paint). **Hard cap of 12 shimmering instances per page**; past that, static placeholders — 12 composited layers costs more than the paint it saves on a low-end device. |
| **`content-visibility: auto`** | offscreen | `content-visibility: auto` + `contain-intrinsic-size: auto 600px` on below-fold sections | n/a | n/a | n/a | No motion. It is a **rendering** optimisation and it exists so that the scroll-driven reveals above stay cheap on a long page. | 0 KB. Removes style/layout/paint for offscreen subtrees. **Do not apply to:** the hero, the LCP image's ancestor, anything inside a `position: sticky` chain, or a page with fewer than 3 long sections. `contain-intrinsic-size` must use the `auto <len>` form or the scrollbar jumps as sizes are learned — that is CLS-adjacent. |

### Scroll-driven work

> **Cross-reference.** A stakeholder asked for a scroll-assembled **3D cube** (the
> effect on `codewiki.google`) and for the category's comparable sites to be
> surveyed. Answered and measured in `MOTION-3D-ANALYSIS.md`. The short version:
> that cube is **three.js / WebGL at ~213.4 KB gz measured** — not CSS 3D — and it
> would exceed this budget by 1.25×–2.1×. The CSS-3D version costs 0 KB but
> **cannot be the LCP element**, consumes this section's six-element budget in one
> object, and costs **20–25 MB of GPU memory** across six render surfaces. **Not
> adopted.** Cheaper alternative priced at 0 KB in §11.5.3. The decision about
> whether it replaces the home page's existing accent moment is open and sits with
> the user (§11.5.4) — **nothing in this section changes until they choose.**

Two techniques, both CSS-native, both with the same three conditions:

1. **`animation-timeline: view()`** for element-level reveals (home sections, PDP
   notes block, tracking markers). Applies to **at most 6 elements per page** —
   each one is a narrative beat and a page with six beats already has too many.
2. **`animation-timeline: scroll()`** with a `position: sticky` frame for the
   PDP story image only.

**Amplitude cap: `translateY` never exceeds 6% of viewport height**, and never
exceeds 24px. A scroll-linked effect whose displacement is visible at a glance
becomes a distraction; at 6% it is felt rather than seen. `opacity` sweeps are
capped at `0 → 1` over the first 35% of entry, never fading *out* — content that
dims as you read it is actively hostile.

**The three non-negotiable conditions:**

- **The default state is the final state.** Every scroll-driven rule lives inside
  `@media (prefers-reduced-motion: no-preference)`. Outside it — and in any
  browser without `animation-timeline` — the element renders at its final
  position. There is no `@supports` query that starts from `opacity: 0`; that
  inversion is how content disappears on Firefox and in Google's crawler.
- **Nothing with `content-visibility` or a scroll timeline may wrap the LCP
  element's ancestor.** A contained subtree is not painted, so the LCP element
  inside it is not painted, and the LCP timestamp moves.
- **`animation-fill-mode: both` is mandatory** on view-timeline reveals, so the
  pre-entry state is defined and there is no one-frame flash of the final state
  before the animation takes hold.

### Route transitions

**There are none. This is the decision, and it is the correct one.**

Cross-route motion was considered and rejected on three grounds:

1. **Every route transition in this app crosses a data boundary.** `/products` →
   `/products/[slug]` is a server render that fetches; `/cart` → `/checkout`
   depends on auth status, which resolves on the client from an in-memory token.
   A transition animation would be animating over a loading state, and the thing
   being animated (the outgoing page) would still be on screen while the new one
   is unknown.
2. **Spec §15.4 requires focus management on every navigation.** A cross-fade
   makes it ambiguous where focus went. The current implementation is correct:
   Next handles focus on route change and the skip link re-targets `main`.
3. **The two transitions that *are* motivated are intra-page**, not inter-page:
   the add-to-cart badge morph and the cart-line reflow (§ the table above). Both
   use View Transitions scoped to a `startViewTransition()` call, which cannot
   leak across a navigation.

The one exception, if it is wanted: `@view-transition { navigation: auto }` for
**cross-document** transitions on `/products` → `/products/[slug]` only, where
both documents are server-rendered and the shared element is the product image.
It is `cross-document-view-transition` support, currently Chrome 126+ only, costs
0 KB, and degrades to an instant navigation everywhere else. **It must not be
enabled site-wide** — it would animate `/checkout` → `/orders/{id}`, which crosses
a payment state. Enable it per-navigation-type, not globally.

### The reduced-motion policy

Enforced at the token layer so a component cannot opt out by forgetting. Three
layers, in order of authority:

1. **The gate.** Every scroll-driven and narrative animation is declared inside
   `@media (prefers-reduced-motion: no-preference)`. This is the *primary*
   mechanism and it is opt-in: to add motion you must write the query. A
   component that forgets simply has no animation.
2. **The token override.** Inside `@media (prefers-reduced-motion: reduce)`,
   every `--motion-*-dur` token is set to `0s` and every `--motion-*-ease` to
   `linear`. One block, in `globals.css`, next to the tokens. Because every
   component reads `var(--motion-enter-dur)`, this catches all of them —
   including ones written after this spec.
3. **The backstop.** The existing blanket rule
   (`animation-duration: 0.01ms !important; animation-iteration-count: 1 !important;
   transition-duration: 0.01ms !important; scroll-behavior: auto !important`)
   is **kept**. It is not sufficient on its own — that is CHANGE-5 — but it is
   correct for keyframe and transition work and it is a real safety net for a
   future dependency that ships its own CSS.

Two things reduced-motion does **not** mean here, and both are deliberate:

- It does **not** mean opacity-0-then-fade. A 0ms opacity transition is a
  flicker. Elements appear at their final state, instantly.
- It does **not** mean the skeleton stops being a skeleton. `--color-line`
  static block is still a skeleton. Only the shimmer stops.

There is also a **`prefers-reduced-transparency`** consideration that does not
apply — the system uses no `backdrop-filter` (R12) — and a
**`prefers-contrast: more`** consideration that *does*: under
`prefers-contrast: more`, `--color-line` hairlines are promoted to
`--color-line-strong` and `--color-bronze-soft` plates are dropped in favour of
`--color-surface`. That is a two-line override and it is in scope.

### Rejected techniques, with the reason

These were considered and cut. Each is listed because a future contributor will
otherwise re-propose it, and because "we tried it and here's why not" is the
cheapest documentation in the repo.

| # | Technique | Why rejected |
|---|---|---|
| **R1** | Hand-rolled `height`/`max-height` animation for the cart line removal | Runs **layout on every frame**. The View Transitions row gets the identical visual result — the browser re-lays-out natively and cross-fades — with zero hand-written layout code and no jank on a 60Hz Android. This is `PLAN.md`'s original proposal, corrected. |
| **R2** | Framer Motion / `motion` (any version) | Every candidate use was re-examined against the component inventory and **none needs layout or gesture orchestration**: the menu is a discrete mount, the toast is a keyframe, the cart line is View Transitions, the checkout step is a discrete swap. The package would buy nothing this spec already reaches with the platform. |
| **R3** | GSAP + ScrollTrigger | **46.3 KB gz combined (measured — see §11.5.2)** — **38.6% of the entire 120 KB first-load JS budget** — to produce scrubbed timelines that `animation-timeline: view()` / `scroll()` produce natively at 0 KB on the target browsers. The fallback for non-supporting browsers is worse than the library: GSAP would animate everywhere, CSS animates only where it can. |
| **R4** | Lenis (smooth scroll) | Hijacks native scroll. It breaks scroll-driven CSS timelines (the two fight over the same scroll offset), breaks scroll restoration on back-navigation, and is a documented WCAG 2.3.3 problem (animation from interactions) when it modifies scroll velocity. 5.4 KB gz measured (§11.5.2) for a net loss. |
| **R5** | Parallax or scale on the hero / LCP image | Defers or perturbs the LCP timestamp on the page where it costs the most money. See the PDP row in the table for the corrected allocation. |
| **R6** | Animated money counters | A rolling number is unreadable at the instant it is read, and it implies intermediate values that never existed. Violates P5 — the figure must be server truth at every moment it is on screen. |
| **R7** | Per-card scroll stagger on the PLP grid | Re-fires on every filter change, i.e. on every 400ms-debounced keystroke. Twelve cards animating in sequence reads as noise and costs twelve composited layers. Communicates nothing: the grid did not arrive, it was re-filtered. |
| **R8** | `backdrop-filter` frosted header | Reads the page behind it **every frame the page scrolls**, on the one element that is always on screen. Plus it is the exact glassmorphism cliché this system exists to prevent. |
| **R9** | Word-by-word / character-by-character headline reveal | Splits the `h1` into per-word spans. It destroys the element's single accessible name (a screen reader announces the words as one run but the DOM is now 12 nodes), and it puts the headline's text in no single text node for a crawler. The visual gain is 400ms of theatre. |
| **R10** | Infinite announcement marquee / ticker | **WCAG 2.2.2 Pause, Stop, Hide is a WCAG 2.1 AA criterion** and a permanently animating banner cannot be paused. It is also, empirically, the loudest element on all four working reference sites. If an announcement is required it ships as a **static** strip with `aria-live="off"`. |
| **R11** | Lottie or Rive hero animation | 50–200 KB of JSON plus either a canvas runtime or a WebGL runtime, for an effect that stills into a poster image. The JSON is not in the static HTML, so the LCP element becomes a canvas that the crawler sees as empty. |
| **R12** | Auto-advancing hero carousel | Spec §3.2's own reference mock shows a static hero, and `PLAN.md` §4 ruled it out. It also churns the LCP element on a timer, and spec §18.1 treats an auto-rotating above-the-fold image as a Core Web Vitals problem. |
| **R13** | Animated checkout step transition (cross-fade or slide between Details → Review → Pay) | The step change moves the **submit button**. Animating across a control that is the target of the interaction which triggered the change puts the button in motion under a cursor that is about to click it. The progress hairline carries the "which step" information instead. |
| **R14** | Spring / physics-based easing on any UI transition | Springs have no defined end state, so their duration is emergent. That makes the reduced-motion override impossible to express as a token, and it makes the motion unpredictable across frame rates. One easing set, defined as curves, forever. |
| **R15** | Animating `width`, `height`, `top`, `left` or `margin` anywhere | §0's rule one. Layout per frame. The one place a height *is* needed (an auto-height disclosure) should use `interpolate-size: allow-keywords` or the `grid-template-rows: 0fr → 1fr` technique — and in this system there is no such disclosure, so the question does not arise. |

---

## 4. Responsive

A breakpoint table with **concrete per-breakpoint layout differences** — the word
"responsive" is not an answer.

Five breakpoints, mapped exactly onto Tailwind v4's defaults so no custom
breakpoint ever ships (spec §15.1 asks for small mobile / large mobile / tablet /
desktop / wide desktop; this is that list). The build already uses `sm: md: lg:`
throughout, so the tokens are names, not new numbers.

| Breakpoint | Range | Nav | Grid | Type scale | Page-specific changes |
|---|---|---|---|---|---|
| **xs — small mobile** | `< 640px` | Full-screen overlay (`MenuOverlay`), hamburger left, wordmark centred, cart right. Account link moves **into** the overlay — at `sm:inline` today the "Sign in" link simply vanishes below 640 with no replacement, which is the one real gap in the current nav | Product grid **2 columns**. Prose measure full-bleed minus 16px gutters. Sticky summary panels unstick and stack below content | `--text-display` clamps to its 44px floor; `--text-h1` to 32px; eyebrows stay 12px; body stays 16px (never 14 — a 14px sans at 7.19:1 on a phone in daylight is the reason carts get abandoned) | **Home:** hero padding `--space-16`; ghost watermark hidden (already `hidden lg:block`, KEEP); New-arrivals 2-up. **PDP:** single column, image first, purchase block second; **`position: sticky` bottom purchase bar** (price + stepper + Add to cart, `--z-sticky`, `env(safe-area-inset-bottom)` padding); notes stack single-column. **Cart:** lines stack — 96px thumb left, name + line-total right, stepper and Remove on a second row (`CartLineItem` already uses `flex-col` on the text column). **Checkout:** one column; **order summary moves *below* the step**, because at 448px a 380px sticky sidebar has no room; step indicator is a 3-dot row plus the current step's label. **Orders:** rows stack — `Order #` / badge on line 1, date + total on line 2, item summary truncated on line 3. **Track order:** timeline markers in the left gutter at 1px, text at 14px; carrier and tracking number in a `dl`, full-width, because a tracking number must be selectable and copyable. |
| **sm — large mobile** | `640–767px` | Same overlay. Account links return to the bar (the current `sm:inline` breakpoint is correct) | Product grid **2 columns** (unchanged — 3 columns at 640px gives 190px cards, too narrow for a bottle to read) | as xs | As xs, plus: the sticky PDP purchase bar shows the price only, with the full stepper+button row in the page — the bar is a shortcut, not the only path. |
| **md — tablet** | `768–1023px` | Overlay still. Wordmark stays centred | Product grid **3 columns**. Prose measure capped at `34rem` and centred-left | `--text-h1` reaches ~40px; `--text-display` ~56px | **Home:** 3-up New-arrivals. **PDP:** two columns — image 50% / purchase 50%; the sticky purchase panel is **off** here (a 380px column at 768px viewport leaves no reading width); the sticky bottom bar **replaces** it. Notes become a 3-up row but each note keeps its own heading — at 3 × 220px a single shared heading would be unreadable. **Cart:** back to `grid-cols-[1fr_360px]`. **Checkout:** two columns — form 1fr / summary 380px, sticky at `--z-sticky`. **Track order:** timeline marker rail moves to a 2px rule with 12px dots. |
| **lg — desktop** | `1024–1279px` | **Inline nav** returns (`md:flex` today — see note). Wordmark centred absolutely, links left, account + cart right | Product grid **4 columns** in `max-w-6xl` (72rem). Prose measure still capped at `34rem` — the cap does **not** relax on wide screens | `--text-h1` reaches 48px; `--text-display` ~72px | **Home:** 4-up New-arrivals; ghost watermark visible (the "A" at `20rem`, `-right-8 top-6`). Section rhythm `--space-32`. **PDP:** two columns with the purchase panel `lg:sticky lg:top-24` (current behaviour, KEEP) and the bottom bar **removed**. The story image becomes the `position: sticky` parallax frame. **Cart / checkout:** sidebar sticky, `--z-sticky`. **Orders:** back to a single row with `ml-auto` on the total. |
| **xl — wide desktop** | `≥ 1280px` | as lg | 4 columns — **not 5**. The container caps at `max-w-6xl`; going wider would make the card-to-card gap smaller than the card's internal padding and the grid would read as a spreadsheet | unchanged; `--text-display` reaches its 80px ceiling | Everything is `max-w-6xl` and centred. The only difference is the viewport gutters grow. **A luxury storefront that fills a 2560px monitor is a catalogue, not a house** — the whitespace at xl is the design, not an absence of content. |

**Notes on the nav breakpoint, which is a CHANGE.** `Header.tsx:90` uses
`md:flex` (768px) for the inline nav while `MenuOverlay` is `md:hidden`. At
768–1023px the header therefore shows "Shop · New arrivals · Sign in · Cart" in
a 768px bar, which fits but leaves no room for the wordmark at `text-xl` plus
both account actions. The inline nav should return at **`lg` (1024px)**, matching
the sticky-purchase and sidebar story: below `lg`, the page is a single column
and the overlay menu is the right navigation for it. This is one breakpoint
change in one file.

**Two rules that are not per-breakpoint but are enforced at every breakpoint:**

- **Touch targets are 44×44px minimum** at `xs` and `sm`; they may shrink to 32px
  at `lg` for the quantity stepper only. Every other control keeps 44px.
  `pagination`'s `h-9 min-w-9` (36px) is **below the floor on touch** and must be
  `h-11 min-w-11` below `lg`.
- **Nothing is hidden by breakpoint that carries information.** The ghost
  watermark, the `.sr-only` account links and the `sm:inline` account cluster are
  the only things that disappear, and each has a real alternative at the smaller
  breakpoint. The PDP's "delivery and returns" line (§5.2) is **not** among them:
  spec §18.1 and a customer who cannot meet a deadline they cannot see.

---

## 5. Per-page layout

One subsection per row of the `STITCH-PROMPTS.md` §2 coverage contract. Each:
sections in order, composition, imagery, motion, and the routes that share it.

**Shared across every page.** A sticky 64px header (`--z-header`, `bg-paper`,
hairline bottom; `--elevation-bar` once scrolled) and a footer
(`grid-cols-[2fr_1fr_1fr_1fr]` at `md`, stacked below). Both are KEEP.
The skip link targets `<main id="main">`, which every page below renders inside.

**Route inventory, verified against `src/app/` (22 route files + `error.tsx` +
`not-found.tsx` + `robots.ts` + `sitemap.ts`).** Every route in the tree is
covered by a subsection below. Nothing is unaddressed.

---

### 5.1 `/` — home — **brand statement and entry to the catalogue**

- **Sections, in order:**
  1. **Hero.** Left-aligned column, `max-w-4xl`. Eyebrow (`--text-label`,
     `--color-bronze`, "Eau de parfum · Small batches"), `h1` in
     `--text-display` with one word in `--color-bronze` italic (the current
     `restraint` treatment, KEEP — it is the most expensive-looking thing in the
     build for zero bytes), then a 36ch sub-paragraph in `--text-body-lg` /
     `--color-ink-muted`, then one solid `--color-ink` button and one
     `.link-underline` text link. Vertically centred at ~62% of the viewport
     height, not 50% — a hero whose optical centre is the true centre always
     reads as sitting low.
  2. **New arrivals** (`GET /api/products/?ordering=-created_at`, 4 results, the
     server's default page size). Section head: `h2` + a `View all` link on a
     `ml-auto`. 4-up at `lg`. Suspense fallback is `GridSkeleton count={4}`.
  3. **House position.** Two columns at `md`: an eyebrow + `h2` left, two
     paragraphs right in `--text-ink-muted` at `--measure`. The existing copy is
     KEEP verbatim — it is the most honest text on the site.
  4. **Fact strip.** "Ships across India · Secure Razorpay payments · 7-day
     returns" in `--text-label` / `--text-small`, separated by `·` glyphs that
     are `aria-hidden`. KEEP. **These are three verifiable facts, not trust
     badges** — each is a statement about how the store actually behaves. They
     are not "Trusted by 10,000+ customers" or a payment-logo row.
- **Not present, deliberately:** no category tiles (no categories endpoint —
  §12-G8), no testimonials (no reviews read endpoint — §12-G9), no newsletter
  signup (no subscribers endpoint on the customer API — §12-G8), no "shop by
  note". Spec §3.2 lists all of these as *recommended* section order; it is a
  merchandising wish-list, and six of its ten sections have no data behind them.
  Recording that is the honest reading, not omitting them silently.
- **Imagery:** the `.noise::before` paper grain and the `text-ghost` "A"
  watermark at `lg`+ (both KEEP). **No hero photograph** — the hero is
  typographic, because the site has exactly one image per product and none of
  them is a campaign asset. A stock-photo hero is banned; an empty campaign slot
  is not a licence to fill it with one.
- **Motion:** the accent moment is the **entrance of the three sections**, in
  reading order, on `animation-timeline: view()` (§3). Nothing else on this page
  moves except the header shadow. Exactly one accent moment (P2).

---

### 5.2 `/products` and `/products/[slug]` — listing and detail

#### `/products` — browse and filter

- **Sections, in order:** page `h1` ("Shop all fragrances") → `FilterBar` →
  result count (`aria-live="polite"`, the current `count` sentence KEEP) →
  product grid → `Pagination`.
- **`FilterBar` composition.** One row at `md`+, wrapping at `xs`. Search
  (left), min/max price + `Apply` (middle, the current explicit-Apply rule is
  KEEP and correct: `apply-coupon` precedent aside, a debounced price filter
  fires a 400 the customer did not cause), sort (right, `ml-auto`). A removable
  category chip renders below when `?category=` is in the URL (KEEP — the chip
  appears only because the URL carries it; there is no picker to render).
- **Grid.** 2 / 2 / 3 / 4 columns (xs/sm/md/lg). `ProductCard` = 4:5 image box
  on `--color-bronze-soft`, `h3` name in Fraunces, price right-aligned in
  `--text-money` with `tabular-nums`, `{size} ml` beneath in
  `--text-ink-muted`. **No star rating, no "X% off", no badge** except the
  `--color-ink` "Sold out" chip (KEEP) — none of those have data behind them.
- **Empty state.** KEEP the current copy ("Nothing matches. Try a different
  search, or widen the price range.") and its "Clear all filters" button. It is
  already correct and it offers the action.
- **Motion:** **no accent moment.** A listing page's job is comparison, and
  comparison is destroyed by things moving (P1's wrong-when clause, in
  application). The only motion is the `.link-underline` hover and the card
  hover-scale. The result-count line cross-fades on `--motion-state` when a
  filter resolves — that is the one state change on the page and it is worth
  acknowledging. Per-card stagger is R7.
- **`PRODUCTS_PAGE_SIZE` is 12** (`config/settings.py:540`), so the 4-column
  desktop grid fills three rows on page 1. The "2 per page" degradation note in
  `PLAN.md` §5 and the sitemap's `~count/2` comment are obsolete.

#### `/products/[slug]` — **the money page**

- **Sections, in order:**
  1. Breadcrumb (`nav[aria-label=Breadcrumb]`, KEEP).
  2. **Image.** One image, `aspect-[4/5]`, `sizes="(min-width: 768px) 50vw,
     100vw"`, `priority`. **This is the LCP element and it does not animate.**
     If the image 404s or is null, `ProductImage` renders the monogram (KEEP).
     **There is no gallery, no thumbnail strip, no dots, no arrows** — the
     `ImageField` is single and the gesture inventory in `PLAN.md` §5 already
     says so.
  3. **Purchase block.** Eyebrow → `h1` → price → `{size} ml` → stock line →
     `AddToCartButton`. `lg:sticky lg:top-24` (KEEP). The stock line becomes
     `--color-success` text with a 6px dot, or `--color-ink-muted` "Sold out"
     (CHANGE-3). **The size is a static line, not a selector** — there is one
     SKU per product (P4).
  4. **Sold out → restock opt-in.** When `stock <= 0`, in place of the button:
     a `--border-field` form and one "Notify me when it's back" button, posting
     to `POST /api/products/{slug}/restock-notifications/`. This is the real
     functional replacement for the banned "customers also bought" carousel
     (§12-G6).
  5. **Fragrance notes.** The `description` string rendered as three
     typographic groups — top / heart / base — **only if the copy actually
     contains those notes.** `Product.description` is one free-text field; there
     is no structured note data (§12-G10). So: the default is the description as
     `--text-body-lg` prose at `--measure`. If and when notes become structured,
     this section becomes three columns collapsing to one (§4). **Do not invent
     a three-column layout around unstructured text** — that is the "three-column
     icon-feature grid" the spec's own negative list bans, wearing different
     clothes.
  6. **Delivery and returns.** One line, `--text-small`, `--color-ink-muted`:
     the dispatch window and the 7-day return window, linked to `/shipping` and
     `/returns`. KEEP the intent; note the copy is currently hardcoded in the
     policy pages and `/api/settings/` returns **only** contact channels
     (`ops/views.py` `api_settings`: `support_email`, `support_phone`,
     `whatsapp_number`, `whatsapp_message`, `instagram_url`) — so the **return
     window cannot be read from settings**. §12-G11. Until it can, the line is
     static copy and must be changed when the policy changes.
  7. **Still-life / house image.** One `--color-bronze-soft` plate with a
     `ProductImage` of the same product, re-cropped, at `--measure` width. This
     is the element that carries the PDP's one accent moment.
- **Accent moment:** the **sticky story-image drift** in §7 — a
  `position: sticky` frame with `translateY` on a `scroll()` timeline, 6% of
  viewport height, `lg`+ only, entirely absent under reduced motion. It earns its
  place because it is the only element that keeps pace with the notes being read.
  Everything else on this page is still except the entrance of the purchase
  block.
- **Sticky bottom purchase bar**, `xs`/`sm` only (CHANGE, `PLAN.md` §4): price
  left, `Add to cart` right, `--z-sticky`, `env(safe-area-inset-bottom)`. It
  appears once the page scrolls past the in-flow purchase block and hides again
  when the in-flow button is in view. `lg` removes it entirely (the sticky
  sidebar takes over) — **never both at once**, or the customer has two identical
  buttons and no reason to prefer either.

---

### 5.3 `/cart` — review and quantity edit

- **Sections, in order:** `h1` → line items → the two-column body.
- **Composition.** `grid-cols-[1fr_360px]` at `lg` (KEEP). Left: line items,
  `divide-y` on `--color-line`. Right: an aside with `--border-field`, no radius,
  `--z-sticky` `lg:top-24` (KEEP), containing: `h2` Summary → **Subtotal
  (labelled an estimate)** → the `CouponForm` → the checkout CTA.
- **Line item** (`CartLineItem`, KEEP structure): 96×120 thumb linking to the
  PDP with `aria-label={product.name}`, name + `{size} ml · ₹x each` left, line
  total right in `--text-money`, then a row with the quantity stepper and
  `Remove`. The stepper is `role="group"` with `aria-label="Quantity for {name}"`
  (KEEP — the per-item label is correct and a bare `aria-label="Quantity"` on
  twelve rows is ambiguous).
- **The money label.** `CartView.tsx:117` says "Final totals are confirmed at
  checkout". With `POST /api/cart/coupon/` available and
  `GET /api/shipping/estimate/` priced, the cart can now show the coupon discount
  for real. The estimate label **stays** — the delivery charge is still not
  knowable until a destination is entered (§12-G3), and the total is still
  server-minted. What changes is that the applied coupon is persisted server-side
  rather than remembered in `sessionStorage` (CHANGE-15).
- **CTA by auth state** (KEEP the three-way branch): authenticated →
  "Proceed to checkout"; anonymous → **"Checkout as guest"** (CHANGE-11, the
  backend is `AllowAny`) with "or sign in" as a `.link-underline` beneath;
  `loading` → a disabled button at 60% opacity. The current anonymous copy is
  "Sign in to checkout" with "Your cart is saved on this device" beneath — both
  lines change.
- **States** (all four already exist and are KEEP): loading `CartSkeleton`
  (3 rows, correct proportions), error → bordered panel + "Try again" +
  `refetch()`, empty → `p-16` panel + "Shop fragrances", populated.
- **Motion:** the **line-removal View Transition** and the **stepper value
  cross-fade**. No accent moment of its own — the removal morph is a state
  answer, not a signature.

---

### 5.4 `/checkout` — address, delivery, review, payment

- **Sections, in order:** `h1` → **step indicator** (3 steps) → step panel →
  order-summary aside.
- **Step indicator** (NEW, `--text-label`). A 1px `--color-line` rule spanning
  the panel with an `--color-ink` `transform: scaleX()` fill whose fraction is
  `stepsCompleted / 2`, plus three labels below it with the current one in
  `--color-ink` and the rest in `--color-ink-muted`. `aria-current="step"` on the
  active label. 180ms `--motion-progress`. This is the one motion on the page
  (R13 forbids the alternative) and it carries a real question — "which step am
  I on, and how many are left" — that the current page does not answer at all.
- **Step 1 — Delivery.** The existing `AddressForm` field-for-field (KEEP): full
  name, **email**, phone, address (full width), city, state, pincode, in a
  `sm:grid-cols-2` grid with `address` spanning both. Email stays: there is no
  `/api/accounts/me/` endpoint to source it from, and Razorpay needs the prefill.
  Below the fields, the **delivery estimate** (NEW): on blur of pincode **and**
  state, `GET /api/shipping/estimate/?state=&pincode=`, rendered as one row per
  option — `{name}`, `{amount}` or "Free" from `free_shipping` — with the
  cheapest preselected, because `shipping/pricing.py` states the precedence
  explicitly: *"across methods the cheapest wins (a customer who expressed no
  preference is choosing by price)"*. A 400 renders as "We cannot deliver there
  yet" in `--color-danger`; the backend's refusal message is already
  customer-shaped.
- **The PII disclosure block** (`AddressForm.tsx`, KEEP verbatim). It is the
  best thing in the frontend: a visible "Prefilled from details saved on this
  device" notice at the point of prefill, a real "Forget saved details"
  control, and a cleared-on-logout key. Spec §17.4 privacy and `PLAN.md` §7 both
  require it. **Not restyled, not shortened, not moved.**
- **Step 2 — Review.** Line items with server prices, subtotal, the applied
  coupon from `CouponForm` (now persisted, CHANGE-15), and the delivery option
  chosen in step 1. `Back` + `Create order`. The creation error renders in
  `--color-danger` with `role="alert"`. **Send an `Idempotency-Key` header** —
  `backend/docs/api-contract.md` documents it as optional and
  `orders/views.py` honours it, returning the original order on replay. KEEP the
  key stable across retries of *this* order attempt.
- **Step 3 — Payment.** Order number (`ORD-YYYY-NNNNNN` — the customer-facing
  reference, `orders/serializers.py:85`), server `total_amount` in
  `--text-money`, the `shipping_amount` line, and the Razorpay launcher. **The
  amount is displayed, never computed here.**
- **Payment phases** (`PaymentLauncher.tsx`) — **KEEP every phase and its exact
  structure**, restyled only:
  | Phase | Treatment | Motion |
  |---|---|---|
  | `ready` | solid `--color-ink` button, `{Pay ₹x}` | none |
  | `working` | `role="status"` text, replaced in place | none |
  | `dismissed` / `verify-error` | `--border-field` panel + `Retry payment` + the "you won't be charged twice" note | none |
  | `maybe-processed` | `--border-field` panel, "check your orders" | none |
  | `captured-dead-end` | **`--border-danger`, `role="alert"`**, "Payment received — order not completed", "**Do not retry this payment**", link to the order. **No retry affordance.** | none |
  | `failed` | `--color-danger` message + `Try again` | none |
- **Money states do not animate.** R13's reason applies with more force here: the
  element carrying "Retry payment" must be available the frame the state changes.
- **Guest checkout** (CHANGE-11). On success the backend returns `guest_token`
  **once**. Storing it is a credential-storage decision with a security review
  attached, not a design decision — §12-G1 records it as an open question for the
  architect, along with the required "save this link to track your order"
  affordance. **The UI must not invent a storage policy.**

### 5.5 `/orders` and `/orders/[id]` — history and live tracking

#### `/orders` — order history

- **Composition.** `ul` with `divide-y divide-line border-y border-line` (KEEP).
  Each row is one `Link` to `/orders/{id}`, laid out as: `Order #{id}` in
  Fraunces `h3`, `OrderStatusBadge`, the date, then `ml-auto` the total in
  `--text-money`; item summary beneath, `truncate`.
- **The reference changes.** `Order.order_number` (`ORD-YYYY-NNNNNN`) **replaces
  the sequential `id` in all customer-facing text** (§12-G4). `id` stays in the
  URL and the API. A customer reading `Order #4` on WhatsApp and the support
  agent reading `ORD-2026-000147` are two different conversations; this closes
  that. The e2e specs (`e2e/orders.spec.ts:39-45`) match on `Order #${order.id}`
  and must be updated in the same commit.
- **`OrderStatusBadge`** (KEEP the component, CHANGE the source of truth). It
  currently keys off `status` alone, which the backend now mirrors from
  `payment_status` and `fulfilment_status` (`orders/serializers.py:102`). The
  redesign renders the two dimensions separately where they differ, because
  "pending payment" and "pending, paid, awaiting dispatch" are different
  customer situations and one badge cannot say both. Tonal treatment is
  unchanged: **no green "success" styling** — `PLAN.md`'s component inventory
  already rules it out, and a green badge on a paid order is decoration.
- **States** (KEEP all four): loading → `role="status"` text; anonymous or 401 →
  bordered panel + "Sign in to see your orders" linking
  `/login?next=/orders`; error → panel + "Try again"; empty → panel + "No orders
  yet" + "Shop fragrances".
- **Motion:** none beyond the row hover. A list of transactions is a ledger.

#### `/orders/[id]` — single order + **live tracking**

- **Composition.** The existing `lg:grid-cols-[1fr_380px]` split (KEEP), now fed
  by `GET /api/orders/{id}/` (CHANGE-13).
- **Sections, in order:**
  1. "← All orders" back link (KEEP, `prefetch={false}` — KEEP, the list refetch
     on arrival is real).
  2. `Payment received — thank you` banner when `?payment=success` **and**
     `status === "confirmed"` (KEEP the `data-testid`, KEEP the guard: the banner
     must not appear on an unconfirmed order).
  3. Header row: `Order {order_number}`, status, date.
  4. **Lifecycle timeline** (NEW). The six lifecycle timestamps the serializer
     already ships — `paid_at`, `fulfilled_at`, `shipped_at`, `delivered_at`,
     `cancelled_at`, `refunded_at` — plus `payment_status` and
     `fulfilment_status`, rendered as a vertical rule with the reached steps
     filled and the unreached ones `--color-line`. **The unreached steps are
     printed with their labels and no date** — a customer can see what is still
     to come, which is the actual question.
  5. **Shipment tracking** (NEW). `GET /api/shipping/track/{order_number}/`.
     One block **per parcel**, oldest first, because a split shipment is
     explicitly modelled (`shipping/views.py:129` — a one-row answer "silently
     dropped every parcel but one"). Each parcel: `status`, `carrier`,
     `tracking_number` (in a `dl`, selectable, `--text-money` numerals so digits
     are unambiguous), `estimated_delivery`, and its `events` as a timestamped
     list. **Only the six customer-facing fields are ever rendered** —
     `internal_note` is not in `ShipmentTrackingSerializer` and the serializer's
     docstring states the omission is the point. Do not add a "warehouse notes"
     field.
  6. Items, `dl` of totals (KEEP the structure), now including the
     `shipping_amount` and `shipping_method_code` lines.
  7. Delivery address in an `<address>` (KEEP the element — it is the correct
     semantic and it gives a screen reader the "not an address, an address"
     signal).
  8. **Resume payment** when `payment_status` is unpaid: `PaymentLauncher` with
     `buttonLabel="Complete payment"` (KEEP, including the `data-testid`).
- **Accent moment:** the **tracking marker fill** on `animation-timeline:
  view()` (§3). It earns its place because it maps reading position onto journey
  position, and the same information is present as text at the top of the page.
- **The 404 shapes are all kept and all mean something different** — the order
  belongs to nobody, it does not exist, or you are signed out. The backend
  already returns a uniform 404 to prevent existence leaks and the UI must not
  try to distinguish them.

---

### 5.6 `/track-order` — **new page, public shipment tracking**

Spec §3.9 names this route and §3.9 line 1189 defines its flow as "a secure,
limited-access token or authenticated account". The backend shipped exactly
that. **This page does not exist in `src/app/` today and is the single largest
addition this redesign makes.**

- **Job:** let a customer who has an order number find out where their parcel is,
  without an account.
- **Composition.** One centred column at `--measure` + 4rem, on `bg-paper`.
  1. `h1` "Track your order".
  2. One form: **Order number** (required) and **Order token** (optional, with a
     visible explanation that a signed-in customer does not need it). Submit →
     `GET /api/shipping/track/{order_number}/` with
     `X-Guest-Order-Token` when a token was supplied.
  3. On a miss, render the backend's answer **verbatim and unstyled**: it is a
     single byte-identical body for "no credential", "wrong token", "no such
     order", "someone else's order", and "no shipment yet"
     (`shipping/views.py:_tracking_lookup_miss`). **The page must not
     differentiate the cases** — any difference turns it back into the existence
     oracle the backend spent audit cycles closing. This is a hard constraint, not
     a styling preference.
  4. On a hit, render the same shipment block as §5.5, shared component.
- **Where does the token come from?** Two honest sources and no third:
  the `?token=` query parameter on the email confirmation (if the backend ever
  adds one — it does not today, §12-G12), or the customer pasting it from the
  link the checkout flow shows them (CHANGE-11). **The page does not accept a
  bare order number and pretend that is authorisation** — the backend will not
  honour it, and a UI that implies otherwise is worse than no page.
- **SEO:** `noindex, follow`. It is a utility surface on a per-customer secret.
  Add to `robots.ts` disallow.
- **Motion:** the tracking markers (shared with §5.5). On first load with no
  query, **no motion at all**.

---

### 5.7 `/returns` and `/shipping` — policy

- **Both** use `LegalShell` (KEEP): `max-w-3xl`, eyebrow, `h1`, "Last updated:
  September 2026", then `space-y-10` of `LegalSection`s, then a footer with the
  support address and cross-links to `/terms` and `/privacy`.
- **`/returns` is a policy page, not a request form.** The coverage contract's
  one-liner — "Return request and status" — is aspirational: there is **no
  customer-facing returns endpoint** (§12-G13; the only returns machinery is
  `POST /api/admin/orders/{id}/refund/`, which is staff-gated). The redesign
  therefore keeps `/returns` exactly as it is — a policy page whose "How to
  request a return" section is a `mailto:` with a pre-filled subject — and
  records the missing endpoint. **Shipping a form that posts nowhere would be the
  worst outcome available**: it would collect a customer's address and reason and
  discard it.
- **`/shipping`** is unchanged in content. Both pages gain exactly one thing:
  the delivery- and return-window facts are stated as a short, scannable pair at
  the top rather than only inside prose, because that is the only thing a
  customer opens these pages to find.
- **Motion: none. Zero accent moments** (P2's wrong-when clause). A legal page
  that animates is a legal page nobody trusts.
- **Both are indexable** (KEEP — no `robots` override on either).

---

### 5.8 `/contact` — contact and enquiry

- **Composition.** `LegalShell` + `ContactLinks` (KEEP both), fed by
  `getContactSettings()` (`GET /api/settings/`, 60s revalidate, env fallback).
  Four sections: reach us directly, customer support, payments, the house.
- **The icons are KEEP.** Hand-drawn inline SVG at `h-5 w-5`,
  `stroke="currentColor"` `strokeWidth={1.5}`, `aria-hidden` on the glyph with
  `aria-label` + `title` on the link. They inherit `currentColor`, so they theme
  with `hover:text-bronze`, and they are 24 bytes each rather than a 40 KB icon
  package. **No icon library, no emoji** (both banned by the negative list).
- **The phone line is conditional** on `contact.supportPhone` (KEEP) — unconfigured
  channels render nothing rather than a dead link. That is correct and it is
  unusual; most sites in this category show every channel always.
- **CHANGE:** the contact buttons get `--border-field` (4.90:1) instead of
  `--border-line` (1.30:1). A 40×40 control whose only affordance is a hairline
  and a glyph is a 1.4.11 failure; the glyph itself passes at 17.49:1, but the
  boundary should too.
- **No form.** Spec §3.9 says "Contact form and support channels". There is no
  contact-submission endpoint and adding one is a backend change (§12-G8). The
  real channels render; that is the whole page.

### 5.9 `/login`, `/register`, `/staff/login`, `/staff/mfa/enroll` — authentication

- **Shared shell.** `AuthShell` is `max-w-md` centred with an `h1` in
  `--text-h1` (KEEP). **`AuthShell` gains one thing: the wordmark.** A 448px
  column of form fields with no brand mark is a login screen from 2009. One line
  of `Fraunces` at `--text-h3` above the `h1`, centred, and nothing else.
- **`/login` — customer door.** `LoginForm variant="customer"` renders
  **username + password and no third field** (KEEP — `lib/mfa.ts` and the
  backend's `StorefrontLoginView` make this correct; a TOTP field on a customer
  door is unknowable noise). The recovery links beneath (`/staff/login`,
  `/register`, `/resend-verification`, `/forgot-username`, `/forgot-password`)
  are **always rendered, never branched on the error** (KEEP — the backend's 401
  is deliberately ambiguous, and branching on the failure reason would leak the
  enumeration signal it exists to withhold).
- **`/register`.** Username (with the live `username-available` debounce), email,
  password. Server field errors map onto fields via `aria-invalid` +
  `aria-describedby` (KEEP; `AddressForm` is the reference implementation). The
  503 case — *account created, verification email failed* — gets its own copy
  and a link to `/resend-verification`. That state is distinct from an error and
  today it surfaces as a generic failure.
- **`/staff/login`** and **`/staff/mfa/enroll`** — **not redesigned.** See the
  out-of-scope decision below.
- **`GuestOnly`** (KEEP): `role="status"` while loading, a plain message while
  redirecting, the form when anonymous. It renders nothing focusable while
  redirecting, which is correct.
- **Motion: none on any auth page.** A form that fades in is a form that
  suggests the fields were not always there.

### 5.10 `/forgot-password`, `/forgot-username`, `/reset-password`, `/verify-email`, `/resend-verification` — recovery and verification

- **All five share `AuthShell` + `EmailOnlyForm` or a dedicated client
  component** (KEEP all).
- **`EmailOnlyForm`** renders one labelled email field and one submit button. On
  success it **replaces the form with the server's message** inside a
  `--border-field` panel with `role="status"` `aria-live="polite"` (KEEP). The
  message is shown **verbatim** because the backend answers uniformly whether or
  not the account exists (`accounts/urls.py`; the uniform-response rule is
  `conventions.md:25`). **Never** branch on it.
- **`/reset-password`** (`uid` + `token` from the query, then a new-password
  field): server `validate_password` errors arrive as `{password: [msgs]}` and
  render **per message**, not as one blob (KEEP — the `ApiError.fieldErrors`
  handling in `lib/api.ts`).
- **`/verify-email`**: posts `uid`+`token` on mount, then shows success, or the
  uniform 400 with a `/resend-verification` link.
- **Motion: none, on all five.** Zero accent moments. These pages are used by
  someone who is locked out; they get the quietest treatment in the system.
- **One thing is added:** a `--border-field` panel around the `EmailOnlyForm`
  success state, so the swap from form to message is a visible container change
  rather than just vanished fields.

### 5.11 `/terms`, `/privacy` — legal

- **Both keep `LegalShell` verbatim.** Long-form prose at `--measure`,
  `--text-body`, `--color-ink-muted`, with `<strong>` terms promoted to
  `--color-ink` (KEEP — `privacy/page.tsx` already does this and it makes the
  defined terms scannable).
- **CHANGE:** add a table of contents. Both pages have 5–7 `h2` sections and are
  the only pages on the site long enough to need one. A plain `<nav>` of in-page
  anchors above the first section, `--text-small`, `xs`-hidden (a 7-item ToC on
  a phone is worse than none), `md`-visible, and `lg:sticky lg:top-24` in a left
  column so the page becomes `grid-cols-[200px_1fr]` at `lg`. **No scroll spy** —
  that would be a JS scroll listener plus an `IntersectionObserver` for a
  navigation aid, and it would need its own reduced-motion story. The ToC does
  not highlight the current section.
- **Motion: none.**

---

### Out of scope: `/admin`, `/staff/login`, `/staff/mfa/enroll`, `error.tsx`, `not-found.tsx`

The coverage contract asks for an explicit decision here, so:

**`/admin` — the Django admin lives on the API origin, not here.**
`admin/page.tsx` is a 5-line `redirect()` to `process.env.ADMIN_URL`. **KEEP it
exactly.** It already has `noindex, follow: false` (KEEP). There is no storefront
`/admin` to design: spec §5 and §13 both place the admin in a **separate
application surface** with its own layout, navigation tree, access control layer
and visual hierarchy, and the backend has already built it — the Django admin
behind an RBAC capability layer (`capability_required` decorators, per-role
`CAPABILITY_ROLES`). Designing a second admin theme in the storefront would
duplicate a surface that exists and does not belong to this brief.

**`/staff/login` and `/staff/mfa/enroll` — role-gated operational surfaces.**
Same reasoning, and stronger here. `/staff/mfa/enroll` is a TOTP enrollment form
(`MfaEnrollForm`, 7.6 KB) whose job is to get a privileged account onto a device.
**The design brief for it is "unmistakable", not "expensive".** Applying
`--text-display`, a paper grain and a 600ms narrative entrance to an MFA
enrollment screen would make a security-critical action look like a marketing
page — and it would be a **downgrade**, because the current design already gets
this right: same tokens, same focus ring, same `inputClass`, same reduced-motion
backstop, `noindex`. **KEEP both as they are.**

The App Router split that makes this correct: both `/staff/*` routes currently
render inside the **storefront** `<main>`, with the customer header and footer
around them. That is a real inconsistency — a staff sign-in page should not carry
the customer nav. The fix is a **route group with its own minimal layout**
(wordmark + form only), **not** a restyle. That is a structural change with a
functional motive, and it is recorded here so it is not mistaken for a design
decision.

**`error.tsx` and `not-found.tsx` — KEEP, with two additions.**

Both are `py-32` centred blocks with a `--text-label` eyebrow, an `h1`, a muted
paragraph and one solid button. Both are correctly restrained. Add:

- `not-found.tsx` gets **four real links** — "Shop all fragrances", "New
  arrivals", "Contact", "Home" — because a dead end on a 404 is a lost session.
  Still no imagery, no illustration, no search box.
- `error.tsx` surfaces `error.digest` in a `dl` under an "If you contact support,
  quote this reference" label. The digest is already in the props and is exactly
  what support needs; today it is discarded.
- Neither gains motion.

---

## 6. Component inventory

Every reusable component with its **loading / empty / error / skeleton** states.
The empty and error states are where most redesigns quietly fail.

Every existing component is listed, plus the six the redesign adds. The four
state columns are the spec §15.3 required list, reduced to the four that are
routinely missing. "n/a" means the state cannot occur for that component and the
reason is given.

**Discipline for this table.** `loading` and `skeleton` are different columns and
they are not interchangeable: `skeleton` is the *visual* placeholder and must
mirror the real layout's geometry, `loading` is the *semantic* state and must be
announced (`role="status"` / `aria-busy`). A component can have a skeleton with
no `aria-live`, or an announced state with no skeleton — but a grid skeleton with
no announced count change leaves a screen-reader customer with silence.

| Component | Used on | Loading | Empty | Error | Skeleton | Notes |
|---|---|---|---|---|---|---|
| `Header` | **all** | n/a — server-rendered shell; the cart count renders `sr-only "0 items"` until the query lands, which is correct | n/a — always has links | n/a — a failed cart query leaves the count at 0 silently, which is *correct* (an error badge in a header is noise) | none | **KEEP.** Add: account links into the overlay at `xs` (§4); nav breakpoint `md`→`lg`; cart count badge from `rounded-full` stays (it is a counter); `view-transition-name` for the add-to-cart morph |
| `MenuOverlay` | **all**, `xs`–`md` | n/a — mounts synchronously on click | n/a | n/a | none | **KEEP the focus trap verbatim.** Add `@starting-style` enter/exit. `--elevation-modal`, `--z-overlay` |
| `Footer` | **all** | n/a — async server component; contact channels arrive from `getContactSettings()` with a 60s revalidate and an env fallback | **Partial**: unconfigured channels render nothing (KEEP — `whatsappNumber`/`supportPhone`/`instagramUrl` are all empty by default) | Silent by design — `getContactSettings` catches and returns `fallbackContact` | none | **KEEP.** `ContactLinks` border → `--border-field` |
| `ProductCard` | `/`, `/products`, `/cart` (thumb) | n/a — server-rendered; the image box is sized so the card never jumps | n/a — a card is never "empty" | **Image only**: `onError` → monogram (KEEP) | n/a — `GridSkeleton` covers it | **KEEP.** CHANGE-8: hover scale 1.04@500ms → 1.03@240ms. Sold-out chip KEEP. **No rating, no badge, no strikethrough** |
| `GridSkeleton` | `/`, `/products` | n/a | n/a | n/a | **is** the skeleton | **KEEP.** Exact column mirror at every breakpoint. `aria-hidden`, shimmer cap 12 (§3) |
| `ProductImage` | `/products/[slug]`, cards, cart, orders | n/a | **Null path** → monogram, `aria-hidden` | **404** → `setFailed(true)` → monogram | n/a | **KEEP.** The two real degradation paths are both handled and both are quiet |
| `FilterBar` | `/products` | Debounced search at 400ms (KEEP); sort applies on change | n/a — an empty result set is the grid's job, not the bar's | **Invalid price** → `role="alert"` in `--color-danger` (CHANGE-2) + `Prices must be numbers.` (KEEP the copy, it matches the backend's 400) | none | **KEEP.** No category picker (§12-G8). Explicit `Apply` for price is KEEP and correct |
| `Pagination` | `/products` | n/a | n/a | n/a | none | **KEEP** `pageWindow()` gap logic and `rel="prev"`/`rel="next"`. CHANGE: `h-9 min-w-9` → `h-11 min-w-11` below `lg` (36px is under the 44px touch floor) |
| `AddToCartButton` | `/products/[slug]` | **idle / adding / added** — `mutation.isPending` → "Adding…", `justAdded` → "Added" for 1.5s (KEEP) | **Sold out** → a muted "Sold out" line (KEEP), replaced by the restock form (§5.2) | `onError` → toast with the server's message (KEEP) | n/a | **KEEP all four states.** Disable on `isPending \|\| justAdded` — the double-submit guard is correct. Add `view-transition-name` |
| `CartLineItem` | `/cart` | **updating** — row at 60% opacity while `patch.isPending` (KEEP) | n/a | `lineError` `role="alert"` in `--color-danger` (CHANGE-2) + `invalidateQueries(["cart"])` reconciliation (KEEP — this is the correct rollback) | none | **KEEP.** Add: View Transition on removal; number cross-fade on step |
| `CartView` | `/cart` | `role="status"` "Loading checkout…" equivalent → **`CartSkeleton`, 3 rows** (KEEP) | **exists and is good** — `p-16` panel, "Your cart is empty.", "Nothing here yet — the shelf is one click away.", → `/products` (KEEP copy) | **exists and is good** — bordered panel, "We couldn't load your cart.", "Check your connection and try again.", → `refetch()` (KEEP) | `CartSkeleton` | **KEEP all four.** CTA copy changes (CHANGE-11): "Checkout as guest" |
| `CouponForm` | `/cart`, `/checkout` | **checking** — button → "Checking…" (KEEP) | n/a — the form *is* the empty state | `role="alert"`; `minimum_order_amount` from `ApiError.extras` renders "Spend at least ₹x to use this coupon." (KEEP — this reads a real server field) | none | **CHANGE-15:** persist via `POST`/`DELETE /api/cart/coupon/`, drop the `sessionStorage` mirror. **KEEP: explicit submit only** — `apply-coupon` is throttled at 10/min and never per keystroke |
| `CheckoutView` | `/checkout` | `role="status"` "Loading checkout…" (KEEP) | **exists** — "Your cart is empty." + `/products` (KEEP) | **exists** — cart error panel + `refetchCart()` (KEEP); `creationError` on the review step with the **404-specific** copy "Your cart session expired. Please add items again." (KEEP — that string is load-bearing; it is the same-site-cookie failure speaking) | none | **CHANGE-11** guest checkout; **CHANGE-12** delivery line; **NEW** step indicator. KEEP the three-step `Step` union and the `saveShipping()` timing — PII is saved only once a working order exists |
| `AddressForm` | `/checkout` | submitting → "Working…" (KEEP) | n/a | `serverError` `role="alert"` + `aria-live="polite"`; per-field errors via `aria-invalid` + `aria-describedby` (KEEP — this is the reference implementation for every form on the site) | none | **KEEP VERBATIM, including the PII disclosure block** (`data-testid="pii-disclosure"`, `data-testid="forget-saved-details"`). **Not restyled.** Add: blur-triggered shipping estimate below the pincode |
| `PaymentLauncher` | `/checkout`, `/orders/[id]` | **working** — `role="status"` with the phase label (KEEP) | n/a | **six distinct error phases** (KEEP all, including the `captured-dead-end` with no retry) | none | **KEEP the `PayPhase` union and every branch.** Restyle only: `border-bronze` → `border-danger`; `text-bronze` → `text-danger`. **Zero motion** (R13). **BLOCKED** by §12-G8 until the CSP is fixed |
| `OrdersListView` | `/orders` | `role="status"` "Loading your orders…" (KEEP) | **exists** — "No orders yet." + `/products` (KEEP) | **exists** — "Couldn't load your orders." + `refetch()` (KEEP) | none (text is correct here) | **CHANGE:** `order_number` replaces `id` in the visible label. Anonymous → "Sign in to see your orders" panel (KEEP) |
| `OrderDetailView` | `/orders/[id]` | **`skeleton`** — a 2-column skeleton matching the real split (KEEP) | **exists** — "Order #N wasn't found." + `/orders` (KEEP) | **exists** — "Couldn't load this order." (KEEP) | yes | **CHANGE-13** `GET /api/orders/{id}/`. **NEW** lifecycle timeline + shipment block. KEEP the `payment-success-banner` `data-testid` and its `status === "confirmed"` guard |
| `OrderStatusBadge` | `/orders`, `/orders/[id]` | n/a | n/a | n/a | n/a | **CHANGE:** read `payment_status` + `fulfilment_status`. **KEEP: no green.** Tonal treatment is `bg-ink` for live, `border-line` for muted (`PLAN.md`'s inventory already rules out green) |
| `AuthShell` | all auth | n/a | n/a | n/a | n/a | **ADD** the wordmark line. Nothing else changes |
| `LoginForm` | `/login`, `/staff/login` | "Signing in…" (KEEP) | n/a | `role="alert"` + `aria-live="polite"`; the staff variant reads `fieldErrors.totp[0]` first (KEEP) | n/a | **KEEP.** Two surfaces, no TOTP on the customer door. **KEEP: recovery links unconditional** |
| `RegisterForm` | `/register` | submitting | n/a | per-field from `ApiError.fieldErrors`; **503 gets its own copy** | none | KEEP + the 503 case (§5.9) |
| `EmailOnlyForm` | 3 recovery routes | pending label per prop (KEEP) | n/a | `role="alert"` (KEEP) | none | **ADD** a `--border-field` panel around the success state. **KEEP: message verbatim, never branched** |
| `ResetPasswordClient` | `/reset-password` | submitting | n/a | `{password: [...]}` rendered per message (KEEP) | none | KEEP |
| `VerifyEmailClient` | `/verify-email` | checking | n/a | uniform 400 + resend link (KEEP) | none | KEEP |
| `MfaEnrollForm` | `/staff/mfa/enroll` | submitting | n/a | per-field | none | **OUT OF SCOPE — KEEP AS IS** |
| `GuestOnly` | `/login`, `/register`, `/staff/login` | `role="status"` "Loading…" (KEEP) | n/a | n/a | n/a | KEEP |
| `LegalShell` / `LegalSection` | 4 policy routes | n/a | n/a | n/a | n/a | **KEEP.** **ADD** the ToC nav. These are static server components; there is no loading state to design |
| `ContactLinks` | footer, `/contact` | n/a | **exists by design** — unconfigured channels render nothing | n/a | none | **KEEP.** CHANGE: border → `--border-field`. Four hand-drawn SVGs, no icon library |
| `toast` | **all** | n/a | n/a | n/a | n/a | **KEEP** `aria-live` container, 3-max, 4s. **ADD** an exit animation and raise to `--z-toast` |
| **NEW `ShipmentTimeline`** | `/orders/[id]`, `/track-order` | `role="status"` "Looking up your parcel…" | **The uniform miss** — the backend's message verbatim, one box, no differentiation (hard constraint, §5.6) | 400/404 both render the same uniform miss; network failure is the only thing that gets a "Try again" | 3 event skeleton rows | **The accent moment for two pages.** Marker fill on `view-timeline`. **Never** renders a field outside the six the serializer exposes |
| **NEW `LifecycleTimeline`** | `/orders/[id]` | part of the order skeleton | All steps unreached — printed with labels, no dates (that *is* the state) | n/a | part of the order skeleton | Reached steps filled, unreached `--color-line`. Reads `paid_at`/`fulfilled_at`/`shipped_at`/`delivered_at`/`cancelled_at`/`refunded_at` |
| **NEW `CheckoutSteps`** | `/checkout` | n/a | n/a | n/a | n/a | `scaleX` progress hairline + `aria-current="step"`. The only motion on the page |
| **NEW `StickyPurchaseBar`** | `/products/[slug]`, `xs`/`sm` | n/a | hidden when the in-flow button is in view | n/a | n/a | `env(safe-area-inset-bottom)`, `--z-sticky`. **Removed entirely at `lg`** — never two live add-to-cart buttons |
| **NEW `TableOfContents`** | `/terms`, `/privacy` | n/a | n/a | n/a | n/a | Static anchor list. **No scroll spy, no observer, no motion** |
| **NEW `restock form`** | `/products/[slug]` (sold out) | submitting | **replaces the add-to-cart button entirely** — a 100%-in-stock catalogue has no state for this component at all | `role="alert"` (throttled at 5/min, so a `429` message must read as "try again shortly") | n/a | `POST /api/products/{slug}/restock-notifications/`. The single-icon `stroke-dashoffset` confirmation (§3) |

**Component count: 30 existing (24 redesigned in place, 6 explicitly untouched)
plus 6 new.** Nothing in the current tree is orphaned by the redesign; the only
deletions are the two CSS rules in DROP.

---

## 7. Accessibility

The non-negotiable floor. A design that cannot meet this is rejected however
beautiful. Target: **WCAG 2.2 AA**, per spec §15.4.

### 7.1 What must not regress

These are all present and correct today. Breaking any of them is a regression,
not a trade-off.

| Guarantee | Where it lives today | The redesign's obligation |
|---|---|---|
| Every text pair ≥ 4.5:1, every non-text ≥ 3:1 | `globals.css` + `ui.test.ts` | KEEP the two-token `line`/`line-strong` split. Re-run `ui.test.ts`. All §2.1 ratios hold |
| Field borders ≥ 3:1, placeholders at full strength | `inputClass` (`lib/ui.ts:16`) | **KEEP VERBATIM.** Do not "simplify" it to one border token |
| A visible 2px focus ring with 2px offset on every interactive element | `globals.css:25` | KEEP the shape; only the colour becomes `--color-focus` (§2.1), which **fixes** a real 2.97:1 failure on dark buttons |
| A keyboard-reachable skip link to `#main` | `layout.tsx:52` | KEEP. The `<main id="main">` it targets must stay on every page |
| A focus trap in the menu overlay, with Escape and focus restore | `MenuOverlay.tsx:26-64` | KEEP **VERBATIM**. Restyle the panel; do not touch the effect |
| `prefers-reduced-motion` honoured globally | `globals.css:117` | KEEP as the backstop; ADD the token override and the `no-preference` gate (§3) |
| Screen-reader announcements for cart updates | `Header.tsx:149` `aria-live="polite"`; `toast.tsx` `aria-live="polite"` | KEEP. The badge count and the toast are two channels for one event; both must survive the redesign |
| Form errors linked with `aria-invalid` + `aria-describedby` | `AddressForm.tsx` | KEEP as the pattern and apply it to every other form |
| JSON-LD script-context escaping | `json-ld.ts` + `json-ld.test.ts` | KEEP. Any JSON-LD added by this redesign goes through `toJsonLdScriptContent` |
| Uniform responses on anonymous flows — the UI never differentiates | `LoginForm`, `EmailOnlyForm`, `ShipmentTimeline` | KEEP and extend to the new tracking page (§5.6) |
| The PII disclosure + forget control | `AddressForm.tsx` | KEEP VERBATIM |

### 7.2 Keyboard order and focus trapping

**Tab order is DOM order, always.** No `tabindex` above 0 anywhere in the system.
The consequence is a layout constraint: **the visual order must be the DOM
order**, which is why the checkout summary is a second `<aside>` in the DOM (and
only *appears* above the step on `xs` via source order per §4 — where it does
move, it moves in the DOM too, not with `order:`).

Per surface:

| Surface | Trap? | Order | Escape | Focus restore |
|---|---|---|---|---|
| Skip link | no | 1st in `<body>`, before the header | n/a | n/a |
| Header | no | menu button → nav links → wordmark → account → cart | n/a | n/a |
| **Menu overlay** | **YES** | first focusable inside the panel on open; Tab and Shift-Tab both wrap | **yes** | to the menu button on close (**KEEP**: `MenuOverlay.tsx:62`) |
| Filter bar | no | search → min → max → Apply → sort | n/a | n/a |
| Quantity steppers | no — `role="group"`, not a composite widget | − → value → +, per group | n/a | n/a |
| **Checkout step change** | no | — | n/a | **moved to the new step's `h2` (`tabindex="-1"`) on every step change.** This does not exist today and it is a real 7.2 gap: today the panel swaps and focus is left on a button that no longer exists |
| Toast | no | never focusable; `role="status"` only | n/a | n/a |
| Payment modal | third-party | Razorpay owns it | n/a | n/a |
| `/track-order` form | no | order number → token → submit | n/a | n/a |

Two traps exist and only one is ours: the menu overlay (KEEP) and Razorpay's.
`PLAN.md` §6 claims a focus trap "in the menu overlay and payment modal" — the
second is not implementable, it is a third-party iframe. **Correct the claim
rather than asserting it.**

### 7.3 Focus visibility

```
outline: 2px solid var(--color-focus);   /* #9c6a2e — 3.76:1 on ink, 4.38:1 on paper */
outline-offset: 2px;
box-shadow: 0 0 0 4px rgb(156 106 46 / 0.22);   /* the halo */
```

The halo is not decoration. The outline alone clears 3:1 against all four
surfaces (§2.1), but a 2px bronze ring on a `--color-ink` button with a
`--color-paper` label is legible in a screenshot and ambiguous in a photograph of
a screen. The 22%-alpha halo separates the ring from the button edge without
adding a second opaque colour that would compete with the label. **The outline
colour must never be set to `transparent` to remove a ring** — if a design cannot
tolerate a ring, it is a hover-only affordance and a 7.3 failure.

`scroll-behavior: smooth` is **off** globally. It is disabled by the reduced-motion
backstop already, and it should be off unconditionally: it is a WCAG 2.3.3
concern and it interferes with the focus ring on in-page anchors (the legal ToC).

### 7.4 Contrast targets

§2.1 is the table; the rules that make it hold are:

- AA normal text 4.5:1, AA large text (≥ 24px, or ≥ 18.66px bold) 3:1, non-text
  and UI components 3:1. The design has **no large-text-only text** — the
  `--text-h1` and below are all ≥ 4.5:1 anyway, which removes the temptation to
  put body copy at 3:1 and call it "large".
- Every interactive component's boundary meets 3:1 (`--border-field`, 4.62–4.90:1).
- The decorative-token exemptions are E1–E3 in §2.1, each with its condition.

### 7.5 Target sizes and pointer

WCAG 2.2 adds **2.5.8 Target Size (Minimum)** at 24×24 CSS px, and 1.4.11 for the
contrast of the target's boundary. This system sits above the AA floor: **44×44
at `xs`/`sm`, 32×32 minimum at `lg`**. Two current violations, both fixed in §4:

- `Pagination`'s `h-9 min-w-9` = 36×36 — passes 2.5.8, fails the 44px touch
  convention on touch. Bumped below `lg`.
- The stepper's `px-3 py-2` on a 14px glyph = ~26×32 — passes 2.5.8, fails 44.
  Bumped below `lg`.

### 7.6 Motion, transparency and contrast preferences

- `prefers-reduced-motion: reduce` — the three-layer policy in §3. **A new
  animation is opt-in**: it must be written inside
  `@media (prefers-reduced-motion: no-preference)`. That is the whole
  enforcement mechanism, and it is checkable by grep.
- `prefers-reduced-transparency` — **no effect.** The system has no
  `backdrop-filter` and no alpha surfaces carrying text (R8). Nothing to honour.
- `prefers-contrast: more` — **in scope, two lines.** Promote `--color-line`
  hairlines to `--color-line-strong` and drop `--color-bronze-soft` plates in
  favour of `--color-surface`. Both improve 1.4.11 and 1.4.3 outcomes and neither
  changes layout, because the plates are decorative fills.
- `forced-colors: active` — the system survives it, which is a direct benefit of
  R8 and of using real borders instead of shadows: hairlines, focus outlines and
  button fills are all expressed in system colours under forced-colors. **The
  `box-shadow` halo in 7.3 will vanish; that is correct** and the outline alone
  must be sufficient, which is why the outline is the opaque part.

### 7.7 Names, labels and structure

- Landmarks: `<header>`, `<nav aria-label="Primary">`, `<main id="main">`,
  `<footer>` — present today, KEEP. The overlay adds `<nav aria-label="Mobile">`
  (KEEP). Every `<nav>` has a **distinct** accessible name: `Primary`, `Mobile`,
  `Shop`, `Help`, `Legal`, `Pagination`, `Breadcrumb`. All present today, KEEP.
- `alt` text: every `ProductImage` gets `alt={product.name}` — the product name,
  not a description, because the image *is* the product. The monogram fallback is
  `aria-hidden` (KEEP). The `.noise` and `.text-ghost` layers are
  `aria-hidden`/`pointer-events-none` (KEEP).
- The cart-count badge's `sr-only "0 items"` (KEEP) means the `aria-label` and the
  live region do not fight. Keep exactly one of them announcing.
- **`aria-live` regions are placed on containers that are already in the DOM.**
  The toast container and the cart-count span are both static wrappers with
  animated or changing children — that is the pattern that survives a visual
  animation. Putting `aria-live` on the animated element itself is the classic
  way to make motion and announcement fight each other.
- The checkout step change announces via focus move (7.2), **not** an
  `aria-live` region: a live region on a step change double-announces against the
  focus announcement.

### 7.8 The accessibility test surface

Six checks, all runnable, none of which are "look at it":

1. `ui.test.ts` — the token pins (§2.1). Extend with a case asserting no
   `placeholder:text-*/NN` opacity modifier and no `border-line ` on a field.
2. A **grep gate**: no `duration-[` or `transition-duration` outside
   `globals.css`; no `z-[` outside `globals.css`; no scroll-driven animation
   outside a `@media (prefers-reduced-motion: no-preference)` block. Three
   greps, three invariants, and they are the whole reason the motion system
   cannot drift.
3. A Playwright assertion that the menu overlay traps focus: Tab from the last
   focusable lands on the first, Escape restores focus to the trigger.
4. A Playwright assertion that **the checkout submit button is focused (or its
   step heading is) after a step change.**
5. A Playwright assertion that every visible text node computes ≥ 4.5:1 against
   its background, walked once on `/`, `/products`, `/products/[slug]`, `/cart`,
   `/checkout`, `/orders/[id]`. This is the automated version of §2.1 and it is
   the only way the table stays true as the design evolves.
6. `emulateMedia({ reducedMotion: 'reduce' })` on `/` and `/products/[slug]`,
   asserting `opacity === '1'` on every scroll-driven target and that the
   skeleton has no running animation.

---

## 8. SEO

Per-page metadata strategy, canonical URLs, Open Graph, JSON-LD types, sitemap
strategy, robots.

### 8.1 Per-page metadata

| Route | Title | Canonical | Robots | OG | JSON-LD |
|---|---|---|---|---|---|
| `/` | `Maison Aurel — Perfumes composed with restraint` (KEEP) | `/` | index | site defaults (KEEP) | `Organization` (KEEP) |
| `/products` | "Shop all fragrances"; "Search results" when narrowed (KEEP) | `/products`, **or** `/products?ordering=…` when only `ordering` is set (KEEP) | **`noindex` when narrowed** (KEEP) | site defaults | none |
| `/products/[slug]` | `{name} — Eau de Parfum {size} ml` (KEEP) | `/products/{slug}` (KEEP) | index | product image + `alt={name}` (KEEP) | `Product` + `Offer` + `BreadcrumbList` (KEEP) |
| `/track-order` | "Track your order" | `/track-order` | **`noindex, follow`** (NEW) | none | none |
| `/cart`, `/checkout` | KEEP | KEEP | `noindex, nofollow` (KEEP) | none | none |
| `/orders`, `/orders/[id]` | KEEP | KEEP | `noindex, nofollow` (KEEP) | none | none |
| `/login`, `/register`, `/staff/*` | KEEP | KEEP | `noindex, nofollow` (KEEP) | none | none |
| all 5 recovery/verify routes | KEEP | KEEP | `noindex, nofollow` (KEEP) | none | none |
| `/shipping`, `/returns`, `/terms`, `/privacy`, `/contact` | KEEP | KEEP | **index** (KEEP) | site defaults | none |

**Two additions to `robots.ts`:**

1. `/track-order` in the disallow list — it is a per-customer-secret surface.
2. `/staff`, `/staff/login`, `/staff/mfa/enroll` — **currently missing.**
   `/staff/login` and `/staff/mfa/enroll` are role-gated and unlinked from the
   storefront nav, which means they are discoverable only via a link in a login
   error message. They must be disallowed. (`/admin` is already there.)

**Canonical + `noindex` on faceted views is the right call and it is already
made.** `products/page.tsx:32` canonicalises to `/products` and sets
`noindex` whenever `isFiltered()` is true. One refinement: `?ordering=` alone is
**not** a crawl trap — it is a user-selected sort of the same page, and the
canonical already preserves it. Keep that behaviour exactly. It is a small,
correct piece of SEO thinking.

**PDP `Offer.availability`** is derived from `stock > 0` (KEEP, `products/[slug]/page.tsx:60`).
The backend has no `in_stock` field (§12-G14), so this derivation is the only
available source, and it is also the reason the UI never displays a numeric stock
count — `BACKEND_REQUESTS.md` P2 says the same thing from the other direction.
**Availability and the page must agree** (spec §18.1): the `Product` JSON-LD
availability, the visible stock line and the add-to-cart state are all driven by
the same `product.stock` value in one place. Do not let them drift.

**Discontinued products** (spec §18.1): `fetchProduct` throws on a 404 and the
page calls `notFound()` (KEEP), which renders `not-found.tsx` with a 404 status.
That satisfies "correct handling of discontinued products". **There is no slug
redirect mechanism** — `Product.slug` changes would 404. No endpoint exists for
it (§12-G15) and none is invented here.

### 8.2 Sitemap

KEEP the implementation and **correct the stale comment.** `sitemap.ts` says
walking all pages costs "~count/2 requests" because the page size was hardcoded to
2. `PRODUCTS_PAGE_SIZE` is **12** (`config/settings.py:540`), so the cost is now
`~count/12`. `revalidate = 3600` is right for a catalogue that changes rarely.

Two additions:
- `/track-order` **excluded**. A secret-keyed utility page has no business in a
  sitemap.
- `/contact`, `/shipping`, `/returns` are **indexable but absent** from
  `staticRoutes` (which today holds only `/` and `/products`). Spec §18.1 asks
  for internal links between pages; a sitemap that lists two of six indexable
  pages is half a sitemap. Add all four policy/help routes at priority 0.4.

### 8.3 How scroll-driven animation coexists with crawling

The specific question. The answer is structural, not a mitigation.

**1. All crawlable content is in the server-rendered HTML.** `/`, `/products`,
`/products/[slug]`, `/shipping`, `/returns`, `/terms`, `/privacy` and `/contact`
are all server components rendering `Product` fields, headings and prose as
literal HTML. There is no `Suspense` boundary between a heading and its text, and
no client component wraps any indexable string.

**2. Motion is declared opt-in, inside `no-preference`.** Every scroll-driven
animation in §3 is inside `@media (prefers-reduced-motion: no-preference)`. A
crawler — and every browser without `animation-timeline` — evaluates that block
as **false** and never applies the animation. The element's *default* style is its
final style. There is no `@supports` guard starting from `opacity: 0`, so the
inversion that hides content from crawlers is structurally impossible.

**3. No text is split.** R9 rejects the word-by-word reveal precisely because it
would put an `h1` into a dozen spans. Every heading is a single text node. A
crawler reads the headline; a screen reader announces it once.

**4. `content-visibility: auto` never wraps an LCP ancestor** (§3). A contained
subtree is not painted, so this would be a rendering optimisation that costs a
crawler nothing but could cost a *Core Web Vitals* measurement its subject.

**5. JSON-LD is server-rendered and script-escaped.** `Product`, `Offer`,
`BreadcrumbList` and `Organization` are emitted in the HTML body on the server
and pass through `toJsonLdScriptContent` (KEEP). Motion cannot affect them.

**6. What a crawler with animations disabled sees, concretely:** the complete
page — hero, product names, prices, descriptions, section headings, all footer
links — with no transforms applied and no opacity below 1. Identical DOM, identical
text nodes, identical links. The only difference is that a transform's `getBoundingClientRect`
is at its final position rather than mid-scroll, which is what a crawler wants.

**The corollary that constrains the design:** if a page's meaning depended on its
animation, it would be uncrawlable and therefore unshippable. That is the reason
P3's second sub-rule ("no animation may carry information that is not also
present as text") exists. It is not an accessibility nicety bolted on; it is the
same rule seen from the SEO side.

### 8.4 What the redesign adds to SEO

| Addition | Route | Why |
|---|---|---|
| `/track-order` | new | Spec §3.9 route, `noindex`. It earns **internal links** from `/orders/[id]`, the footer and `not-found.tsx` |
| `Product` JSON-LD gains `sku`? | — | **No.** `ProductSerializer` is a pinned field whitelist (`products/serializers.py:10`) and adding a field is a backend change. Not proposed. |
| `BreadcrumbList` on `/products` | `/products` | Currently PDP-only. A PLP breadcrumb ("Home / Shop all") is cheap and the type already exists. **Optional — do it only if it earns visual weight at `lg`**, because a breadcrumb on a page with one ancestor is decoration |
| `WebSite` + `SearchAction` on `/` | `/` | There is a real search box (`FilterBar`) and a real `?search=` parameter. One JSON-LD block on `/` and a `SearchAction` pointing at `/products?q={search_term_string}` — but **the parameter is `search`, not `q`** (`filters.ts`, `products/views.py:37`), so the template must use `search`. **Verify the backend accepts `q` before emitting it**; if not, use `?search=` and accept that it is not the canonical `SearchAction` form. Recorded as a judgement call, not a certainty. |
| `breadcrumb` + `order_number` in visible copy | `/orders/[id]` | Not SEO — consistency. See §5.5. |

---

## 9. Testing plan

Which flows get Playwright e2e coverage (only flows with real logic or risk —
cart math, coupon apply/remove, checkout through to the payment handoff, form
validation), which utilities and hooks get Vitest unit tests, and explicitly
**what gets no test and why** (purely presentational components).

### 9.1 Vitest — unit (existing 7 files, all KEEP, plus 3 new)

Existing, all pure and all worth their keep:

| File | Covers |
|---|---|
| `api.test.ts` | `apiFetch` — the 401 → single-flight refresh → retry → logout chain, error-shape normalisation, CSRF header attachment |
| `money.test.ts` | `formatINR` (`en-IN`, INR, API decimal strings) |
| `filters.test.ts` | `parseFilters` / `filtersToParams` / `filtersToPath` round-trip, `isInvalidPrice`, ordering whitelist |
| `redirect.test.ts` | `safeNext` open-redirect rejections |
| `json-ld.test.ts` | script-context escaping — **a security test, not a formatting one** |
| `mfa.test.ts` | `buildLoginBody` / `loginEndpoint` — the customer body can never grow a `totp` key |
| `tokens.test.ts` | refresh single-flight, session hint, logout clearing |
| `ui.test.ts` | the `inputClass` AA token pins (**extend**, §7.8) |

New:

| New test | Covers | Why it earns a test |
|---|---|---|
| `shipping-store.test.ts` | `loadShipping` / `saveShipping` / `forgetShipping` — including the malformed-JSON and partial-object branches that already exist in the source | It is **PII**. The current implementation validates `full_name` and coerces every other field; that validation is the whole point and it is currently untested |
| `motion-tokens.test.ts` | asserts every `--motion-*-dur` token is `0s` under `prefers-reduced-motion: reduce`, and that no scroll-driven `animation-timeline` appears outside a `no-preference` block | §3's policy is the load-bearing accessibility guarantee of this redesign. Making it a test is the only way it survives the next contributor |
| `guest-token.test.ts` (once §12-G1 is decided) | the credential-storage contract for the guest order token | Same reason as above: it is a secret and its storage rule must not drift |

**No component rendering tests.** `vitest.config.ts` sets `environment: "node"`
and there is no `jsdom` or `@testing-library/react` in the tree. Adding a DOM
test environment would add two dev dependencies to test presentational markup
that Playwright already covers in a real browser with real focus semantics.
**Recommendation: do not add one.** If a component needs a unit test, the logic
belongs in a `lib/` module and gets tested there — which is the architecture the
tree already follows (`api.ts`, `filters.ts`, `money.ts` are all extracted
precisely so they could be).

### 9.2 Playwright — e2e

`playwright.config.ts`: `workers: 1`, `fullyParallel: false`, 240s timeout,
`baseURL http://localhost:3000`, trace retained on failure. **KEEP all of it** —
the serial execution is correct against a single seeded Django dev database.

**Two existing specs are stale and will fail against the current backend. Fix
them in the same commit as the redesign, and record the fix:**

1. `e2e/helpers.ts:17` posts to `/api/accounts/login/`, which
   `accounts/urls.py` documents as **the staff door** (`LoginView`, carrying the
   TOTP requirement). The customer door is
   `/api/accounts/storefront/login/`. `e2e_customer` authenticating through the
   staff door is wrong as of SPEC-20-10.
2. `e2e/helpers.ts:22` writes the refresh token to
   `localStorage.setItem("aurel.refresh", …)`. `lib/tokens.ts` is explicit that
   the refresh token **is an HttpOnly cookie and never enters JS-readable
   storage** (SPEC-17-02, R-17.12); `hasSessionHint()` reads the key
   `aurel.session`, not `aurel.refresh`. The helper sets a key nothing reads, so
   the "authenticated" boot state it intends to create does not happen.
   **The fix is to establish the session the way a browser does** — POST the
   customer login through `page.request` so the `Set-Cookie` lands in the shared
   jar, then let `AuthProvider`'s `aurel.session` hint + refresh probe do its
   job. No token in JS storage.
3. `e2e/orders.spec.ts:39-45` matches on `Order #${order.id}` and the literal
   `₹14,000.00`. Both change under §5.5 (`order_number`) and under shipping
   pricing (§12-G3). These assertions must move with the design, or the specs
   will pass/fail for the wrong reason.

New specs, in priority order:

| # | Spec | Covers | Risk it retires |
|---|---|---|---|
| 1 | **checkout e2e, guest path** | guest adds to cart → `/checkout` **without signing in** → address → delivery estimate → create order → `guest_token` returned → pay handoff | The single highest-value change in the redesign (CHANGE-11). If guest checkout silently 401s or the token is dropped, we shipped a broken headline feature |
| 2 | **coupon persistence** | apply on `/cart` → **hard reload** → the coupon is still applied → remove → gone | The bug CHANGE-15 fixes. `sessionStorage` fails this today |
| 3 | **shipping estimate** | valid pincode → priced options render; unserviceable pincode → the uniform refusal in `--color-danger`; no option is invented client-side | The "no silent zero" rule in `shipping/pricing.py` — a refusal must never render as free delivery |
| 4 | **order tracking** | authenticated order with a shipment → the timeline renders every parcel; anonymous with no token → the uniform miss, verbatim | Split-shipment correctness (a one-row UI would pass a single-parcel test and fail the real case) and the enumeration-oracle constraint |
| 5 | **restock opt-in** | sold-out PDP → the notify form submits → the check confirmation appears | The new component's happy path |
| 6 | **reduced motion** | `emulateMedia({reducedMotion:'reduce'})` on `/` and `/products/[slug]` → every scroll-driven target has `opacity === '1'`; the skeleton's animation is not running | §7.8 check 6 — the one automated guard on the motion system |
| 7 | **focus management** | menu overlay traps and restores focus; **the checkout step change moves focus to the new step heading** | §7.8 checks 3 and 4 |
| 8 | **contrast sweep** | walk every visible text node on 6 routes, compute the ratio against its background, assert ≥ 4.5:1 | §7.8 check 5 — keeps §2.1 true as the design evolves |
| 9 | **cart line removal** | remove a line → the remaining lines close up → the total updates | The View Transitions row in §3 |

### 9.3 What gets no test, and why

| Not tested | Why |
|---|---|
| `Footer`, `ContactLinks`, `Pagination`, `GridSkeleton`, `LegalShell`, `OrderStatusBadge`, `ContactLinks` icons | Purely presentational. No branching on user input, no async, no money. The contrast sweep (#8) and the a11y greps (§7.8) cover the ways they can actually be wrong, and a snapshot test of them would fail on every copy edit while catching nothing |
| Every CSS animation | Reduced-motion is covered by #6 and the greps in §7.8. Asserting that a transform equals `translateY(16px)` tests the implementation, not the outcome |
| `/terms`, `/privacy`, `/shipping`, `/returns` prose | Static server components with no data. A test that asserts the presence of a sentence is a change-detector |
| `robots.ts` / `sitemap.ts` output shape | Next's `MetadataRoute` types already enforce it; a runtime assertion duplicates the compiler |
| Razorpay's modal internals | Third-party. We assert that the script is requested and the handler is wired — see below |
| **The payment gateway actually charging money** | The existing `checkout.spec.ts` asserts up to the handoff and tracks the `payment/` and `payment/verify/` requests by URL and body — deliberately without driving the iframe. **Keep that boundary.** `conventions.md:32` requires tests not to hit the network, and an automated real charge is not a test, it is an incident. Test-mode keys, asserted at the seam |
| **The payment step at all, until §12-G8 is fixed** | The CSP blocks the gateway script. Writing a spec for it now means writing a spec that cannot pass. §12-G8 is the blocker; the spec follows the fix |

---

## 10. Performance budget

Targets, per-route JS budget, image format and sizing strategy, font loading, and
what is deferred until interaction (e.g. the payment script loads at the payment
step, never globally).

### 10.1 Targets

Core Web Vitals, from spec §18.2 — these are the spec's own numbers, not
aspirations:

| Metric | Target | Enforced by |
|---|---|---|
| **LCP** | ≤ 2.5s (75th percentile) | the hero/PDP bottle is the LCP element, gets `priority`, an explicit `aspect-[4/5]` box, **and no animation** (§3). Nothing above the fold animates in. |
| **INP** | ≤ 200ms | no scroll listeners except the header's one (which re-renders once per threshold crossing); no per-frame JS anywhere on this site (§3 costs column). The only JS motion is the two View Transitions, each ~12 lines |
| **CLS** | ≤ 0.1 | every image in an explicit aspect box; two size-stable grid skeletons; `tabular-nums` on money so a re-render does not move digits; `next/font` metric-adjusted fallback |

Lighthouse, mobile-emulated, on the production build:

| Category | Target |
|---|---|
| Performance | **≥ 90** (up from `PLAN.md`'s ≥ 85 — the redesign adds motion and must not cost the score; if it does, §3's rejected list is where the cuts come from) |
| Accessibility | **≥ 100** — it is 100 today and this redesign adds two new pages and a new motion system. A drop below 100 is a defect, not a trade-off |
| Best Practices | ≥ 95 |
| SEO | 100 |

### 10.2 Per-route JS budget

Baseline first-load JS on `/`, `/products`, `/products/[slug]` is **≤ 120 KB
gzipped** — the figure `PLAN.md` §6 already committed to, and it is the number
this redesign is measured against. The redesign's contribution is stated as an
increment on top of that baseline, because the baseline itself was never measured
by me (see §13).

| Route | Budget (gz) | What it adds above the baseline |
|---|---|---|
| `/` | ≤ 120 KB | + the View Transitions wrapper, **only if it is on this route** — it is not (the add-to-cart button is on the PDP). **+0 KB** |
| `/products` | ≤ 120 KB | +0 KB. No accent moment, no stagger (R7). The `FilterBar` client island already ships |
| `/products/[slug]` | ≤ 124 KB | **+ ~0.4 KB** — the `startViewTransition` wrapper, loaded **only** when an add-to-cart mutation resolves |
| `/cart` | ≤ 126 KB | + ~0.4 KB (the same wrapper, for line removal) |
| `/checkout` | ≤ 132 KB | + ~0.4 KB wrapper. **Razorpay's ~50 KB is NOT in this figure** — it is injected at the pay step only |
| `/orders`, `/orders/[id]` | ≤ 128 KB | + ~0.4 KB for the tracking marker animation. Bounded by shipment count |
| `/track-order` | ≤ 112 KB | the *smallest* authenticated-free page. No `Header` cart query beyond the existing one, no order list |
| `/login`, `/register`, recovery ×5, `/terms`, `/privacy`, `/shipping`, `/returns`, `/contact` | ≤ 118 KB | +0 KB. **No motion on any of them** |

**The rule that makes the budget real:** the two View Transitions are the only new
client code, they are **~12 lines each**, and they are imported **dynamically** at
the call site so a route that never triggers one never pays for it. That is the
whole cost of "animation-heavy" in this redesign: **under 1 KB of JavaScript.**

**Deferred until interaction — the existing discipline is correct and is
KEEPED:**

| Deferred | Loaded when | Never |
|---|---|---|
| Razorpay Checkout.js (~50 KB, third-party) | `loadRazorpay()` fires inside `PaymentLauncher.startPayment()` (`lib/razorpay.ts:14`) | **never globally.** `PLAN.md` §2 and §6 both state this and it is the single most important perf decision in the build |
| The View Transitions wrapper | the mutation resolves | never on initial load |
| The shipping estimate | pincode + state blur on `/checkout` | never on mount — `shipping/views.py` explicitly notes it needs no throttle scope because it is a read, but a request per keystroke would still be wrong |
| The restock form | the PDP is sold out | never on an in-stock PDP |
| `username-available` | 400ms debounce on `/register` | never on mount |

### 10.3 Images

| Rule | Value | Why |
|---|---|---|
| Format | AVIF first, WebP fallback, via `next/image`'s default `formats` | Spec §18.2 "responsive image sizes and modern formats". **Add `formats: ['image/avif', 'image/webp']` to `next.config.ts`** — this is a config default that is currently unset, so `next/image` serves the original. One line, and it is the single largest image win available |
| Aspect box | every image inside an explicit `aspect-[4/5]` (product) or a fixed `h-28 w-24` (cart thumb) | **CLS ≤ 0.1 is impossible without this.** KEEP — it is already in `ProductCard`, `products/[slug]` and `CartLineItem` |
| `sizes` | `(min-width: 1024px) 25vw, (min-width: 640px) 33vw, 50vw` on cards; `(min-width: 768px) 50vw, 100vw` on the PDP (KEEP) | correct for the §4 grid. **Must be re-verified if the grid changes** |
| `priority` | the PDP bottle and the **first two** cards on `/` (`page.tsx:37`, `index < 2`) | `priority` preloads; more than 2–3 preloads per page makes them compete and none of them is the LCP |
| Below the fold | no `priority`, default lazy | Spec §18.2 "Lazy-load below-the-fold media". **The §3 view-timeline reveals do not change this** — a revealed element is still lazy-loaded, and its reveal animation starts from whatever the browser has already painted |
| Below-fold images | `content-visibility: auto` + `contain-intrinsic-size: auto 600px` on the enclosing section | §3. Bounded: ≤ 8 images per page |
| Broken / null | `ProductImage` monogram fallback (KEEP) | media serving in production was a live P2; the fallback is load-bearing, not decorative |
| `remotePatterns` | the API origin only (KEEP, `next.config.ts`) | one host, no wildcard |

**On the noise texture:** `.noise::before` is an inline SVG data URI, ~340 bytes,
decoded once and cached by the browser as a paint source. It is free and it is
KEEP. Note that it sits on a `position: relative` wrapper with `overflow-hidden`
— that creates a containing block, and a `content-visibility` section must not be
its ancestor, or the noise will not paint. **Do not nest these two techniques.**

### 10.4 Fonts

Per §2.2: two self-hosted variable `woff2` files, `subsets: ["latin"]`,
`display: "swap"`, metric-adjusted fallbacks, preloaded on every route because
both are used in `layout.tsx`. Estimated **35–55 KB gz each**, so roughly
**70–110 KB** of font. Fonts are a **separate budget from JS** and are not
counted against the 120 KB. No `preconnect` — there is no third-party font
request to preconnect to.

### 10.5 The CSP is a performance control

`next.config.ts` sets `script-src 'self' 'unsafe-inline'`, `connect-src 'self'
<apiOrigin>`, `img-src 'self' <apiOrigin> data:`, `font-src 'self'`. Every one of
those is a load-bearing restriction, and the redesign must not widen any of them
to make a feature work:

- **No CDN for fonts** — they are self-hosted.
- **No third-party script** — and this is what makes §12-G8 a real blocker
  rather than a "just add the domain" chore. `script-src 'self'` blocks scripts
  fetched from a *third-party origin*, which is exactly and only what
  `src/lib/razorpay.ts` does. Widening it to
  `https://checkout.razorpay.com` also requires `frame-src`/`child-src` for the
  gateway's iframes and `connect-src` for its API — a materially weaker CSP, and
  a decision that belongs to the user and the backend, not to a design spec.
  **CORRECTED 2026-10-03:** an earlier draft of this line read "which is precisely
  why §11's total is 0 KB". That inference was **wrong** — see §11.5. It
  is escalated, not resolved here.
- **Every animation in §3 is pure CSS**, so `style-src 'unsafe-inline'` (already
  present, required by Next's inline critical CSS) is sufficient and no new
  allowance is needed. **This is the concrete payoff of the CSS-first position:
  the motion system costs the security posture nothing.**

---

## 11. Dependency budget

Every proposed package. The current tree has **no animation library** — verified
against `frontend/package.json`, whose only runtime dependencies are
`@tanstack/react-query`, `next`, `react` and `react-dom`.

### 11.1 The table

| Package | KB (gz) | Route-scoped? | Why CSS cannot do it | Alternative if dropped |
|---|---|---|---|---|
| *(none)* | **0** | — | — | — |

### 11.2 Total

```
NEW RUNTIME DEPENDENCIES ADDED BY THIS REDESIGN:  0 KB
NEW DEV DEPENDENCIES:                              0
TOTAL CHANGE TO frontend/package.json:            0 lines
```

**Is zero acceptable?** Yes, and it is the correct answer rather than a
cop-out. The position is stated at the top of this file — CSS-first is an
engineering position — and this section is where it is either justified or
quietly abandoned. Here is the justification, requirement by requirement:

| Requirement the brief implies | Met by | Bytes |
|---|---|---|
| section entrances as you scroll | `animation-timeline: view()` | 0 KB |
| scroll-linked parallax | `animation-timeline: scroll()` + `position: sticky` | 0 KB |
| a menu that opens and closes | `@starting-style` + `transition-behavior: allow-discrete` | 0 KB |
| a toast that slides in | `@keyframes` (already shipped, `globals.css:32`) | 0 KB |
| a loading shimmer | `::after` + `transform` (CHANGE-7) | 0 KB |
| the add-to-cart button becoming the cart badge | View Transitions API, ~12 lines | ~0.4 KB |
| cart lines closing up on removal | View Transitions API, ~12 lines | ~0.4 KB |
| a checkout progress indicator | `transform: scaleX()` | 0 KB |
| a tracking timeline filling as you read | `animation-timeline: view()` | 0 KB |
| reduced-motion honoured everywhere | a `@media` block + token overrides | 0 KB |

**The two View Transitions wrappers are the only new JavaScript in this
redesign, and they are hand-written rather than imported.** That is deliberate: a
library for `document.startViewTransition()` would be a dependency whose entire
API surface is one function the platform already provides.

### 11.3 What was rejected, and what it would have cost

Published order-of-magnitude figures, **not measured in this repo** (§13, A-7).
Treat them as the reason for the decision, not as a budget line.

| Rejected | ~KB (gz) | Route-scoped? | Why it was rejected | Cheaper alternative — which is what ships |
|---|---|---|---|---|
| **GSAP core + ScrollTrigger** | **46.3** | would have to be (home + PDP + tracking) | **38.6% of the entire 120 KB first-load budget** (measured: 28.3 + 18.0), spent producing scrubbed timelines that `view()`/`scroll()` produce natively at 0 KB on Chrome 115+/Safari 26+. And its fallback behaviour is *worse*: GSAP animates everywhere including browsers that could have had the cheap CSS path | `animation-timeline: view()` + `scroll()`, behind `no-preference`. Loses: scrub on Firefox. Gains: 46 KB, and Firefox gets the static page |
| **`motion` (Motion One / Framer Motion successor)** | **46.7** full build, measured | would have been global for the `mini` build | Every candidate use was re-examined against the §6 inventory and **none needs layout or gesture orchestration**. The one row that looked like it did — the cart line removal — is better served by View Transitions, which re-lays-out natively. **39% of the whole budget for that** | Platform APIs. The `mini` build's only unique capability here would be spring physics, and R14 rejects springs because they have no defined end state, which makes the reduced-motion override impossible to express as a token |
| **Lenis** (smooth scroll) | 5.4 | home + all | R4: hijacks native scroll, fights the scroll-driven CSS timelines for the same offset, breaks scroll restoration, and is a WCAG 2.3.3 concern | Native scroll. It is already smooth |
| **three.js** (for a 3D scroll effect) | **419.7** unminified shipped; **~213.4** measured in Google's Closure-compiled production bundle | would have to be route-scoped, but see §11.5.3 | **125–210% of the entire budget**, a continuous rAF loop, and it cannot be the LCP element. Google's own page spends 504.6 KB gz of JS in total | Nothing. `MOTION-3D-ANALYSIS.md` §5.6 prices the flat-grid alternative at 0 KB |
| **An icon library** (Lucide, Heroicons, react-icons) | ~15–40 tree-shaken, or 0 if SVG-sprited | n/a | The tree has **four hand-drawn inline SVGs** totalling ~2 KB of markup, inheriting `currentColor` and `strokeWidth={1.5}`. A library buys a consistent icon *set*, and the system needs four glyphs | The existing inline SVGs (KEEP, §5.8) |
| **A carousel library** | ~8–15 | products + home | The product model has one image. A carousel would be a UI for data that does not exist | Nothing |
| **A DOM test environment** (`jsdom` + `@testing-library/react`) | dev-only, ~0 prod | n/a | §9.1: `environment: "node"` is correct because every testable unit of logic already lives in a `lib/` module. Adding a DOM environment tests markup Playwright covers in a real browser with real focus | Vitest on `lib/`; Playwright for components |
| **An animation-audit dev dependency** | dev-only | n/a | §7.8's three greps and one Vitest case cover the same ground, are readable in a code review, and cannot be silently updated | Greps + `motion-tokens.test.ts` |

### 11.4 If the client overrides this

The brief said "animation-heavy". If a stakeholder reads that as *more motion than
this*, the honest answer is: **it is already the maximum that fits.** The constraint
is not the aesthetic — it is the 120 KB budget, which a single animation
library would consume ~~a quarter of~~ **38.6% of** (§11.5.2, measured). The CSP
places **no constraint here at all** (§11.5.1) — the original draft of this
paragraph cited it, and that was wrong. The two levers that would actually buy more
motion, in the order I would propose them:

1. **Widen the budget** (client decision) → GSAP becomes affordable and Firefox
   gets scrubbed timelines. Costs ~~31 KB~~ **46.3 KB gz** and a new failure mode.
2. **Widen the CSP** (client + backend decision) → **no benefit for animation**, and
   a real cost in security. Only the Razorpay fix needs it (§12-G8).

Neither is a decision a design spec should make. If the answer is still "not
enough", the correct move is more *moments* (there are currently 6, one per page)
rather than more *motion per moment*.

#### 11.4.1 Quantitative — "the client wants more motion at the cost of optimisation"

Answered in full in `MOTION-3D-ANALYSIS.md` §10–§13, with measurements and with
every model flagged. Summary:

| Question | Answer |
|---|---|
| **Cost of pivoting later** | **~250 lines across 5 files, 6 builder-sized tasks** — and only because §2.4 centralises motion into tokens and §3 enforces reduced motion at the token layer. Under per-component motion the same pivot would touch **~31 files** and would rely on 20 separate chances to honour the reduced-motion gate. **≈ 6× cheaper, and the accessibility guarantee becomes a language feature instead of a convention.** *(Code delta is an order-of-magnitude estimate — A-28.)* |
| **What is genuinely expensive to pivot** | Anything entangled with **layout** (3–5 components + a11y re-verification), with the **LCP element** (a decision reversal, not a code delta — no budget fixes it), or with the **audit surface** (§7.8's greps check CSS and go blind to a JS timeline). |
| **Cost of doubling 120 → 240 KB** | **+12 ms** on this machine *(measured)*; **+0.60 s** at a 1.6 Mbps model constant; **+2.40 s** at a 400 Kbps model constant. **A 218× spread — and I am not averaging the two conditions.** *(Rates are model constants, A-23; the 280 KB transfer slope behind them is measured.)* |
| **What doubling does *not* buy** | **INP, main-thread time, composited layer count, the accent-moment count, or the LCP rule.** All separate axes. More bytes makes INP and main-thread time *worse*. |
| **Are theatre and speed in conflict?** | **Mostly not — the constraint is sequencing, not bytes.** §3's "the default state is the final state" rule means the static page is already the finished page, so a deferred moment needs no loading state, no skeleton and no layout shift. A **46.3 KB deferred chunk costs 0 KB of first load**, versus +2.4 s if the same 46 KB sat on the critical path. |
| **The residual gap at 0 KB** | **One technique: continuous scroll-position scrub** (as opposed to a threshold-triggered staggered reveal). Named precisely rather than inflated. |
| **What doubling to 240 KB would actually build** | §3's CSS system (0 KB) + the two View Transitions (0.4 KB) + **deferred** GSAP/ScrollTrigger on exactly three surfaces (46.3 KB, after LCP, never on the critical path) — and **~73 KB left unspent**, because nothing else on the list is worth it. |
| **Recommendation** | **None of them, as things to buy now. The client does not need to spend anything.** 6 of the 8 baseline technique classes cost 0 KB, and the cube's actual narrative — parts arriving in sequence, forming a whole — is reachable at 0 KB. **If the client insists, buy the deferred 46.3 KB and leave the rest unspent.** |

**What is actually rationing this design is not the budget.** P2 (one accent moment
per page), §3's ≤ 6 simultaneous elements and 6% amplitude cap, and the LCP rule
are all **cognitive and physical constraints that no amount of money overrides**.
The budget rations the *least* valuable thing on the list.

### 11.5 Corrections from the 3D feasibility study — 2026-10-03

Added after `MOTION-3D-ANALYSIS.md`. Three corrections. **Two of them make this
section's conclusion stronger, not weaker, and one retracts a stated reason.**

#### 11.5.1 RETRACTION — the CSP is not a constraint on bundled dependencies

§10.5 previously claimed the CSP was "precisely why §11's total is 0 KB". **That
was wrong, and it is retracted.**

`next.config.ts:16` sets `script-src 'self' 'unsafe-inline'`. `'self'` means the
document's own origin. **An npm dependency bundled by Next is emitted into
`/_next/static/chunks/*.js` and served from the app's own origin** — same-origin,
therefore satisfying `'self'`, therefore requiring **no CSP change whatsoever**.

GSAP, motion, lenis or three.js would all be permitted by this CSP exactly as they
are permitted today. The CSP constrains **where third-party code may be fetched
from**. It does **not** constrain **how much first-party bundled code may ship**.

> **The only real constraint on an animation dependency is the 120 KB per-route
> first-load JS budget (§10.2)** — plus the per-frame, layer-count and GPU-memory
> costs that no byte budget measures, which are set out in
> `MOTION-3D-ANALYSIS.md` §5.

What the CSP genuinely still governs, and what is unchanged: **§12-G8 remains a
real P0.** Razorpay is fetched from a third-party origin and genuinely cannot load
today. That finding is untouched by this correction.

The §11.2 argument needs none of this. It was always a claim that **the platform
satisfies all ten requirements**, which was true independently of the CSP and is
still true.

#### 11.5.2 Measured sizes replace the A-6 estimates

Every order-of-magnitude figure in §11.3 has been replaced with a measured one.
Method, provenance and the category caveats are in `MOTION-3D-ANALYSIS.md` §0 and
§4: registry metadata via `npm view`, shipped builds downloaded from unpkg and
gzipped locally with `GZipStream(Optimal)`.

| Package | §11.3 said (**E**) | Measured (**M**) | Share of 120 KB | Effect |
|---|---|---|---|---|
| gsap + ScrollTrigger | ~31 KB gz | **46.3 KB gz** (28.3 + 18.0) | **38.6%** | rejection **stronger** |
| motion (full build) | ~18–34 KB | **46.7 KB gz** | **38.9%** | rejection **stronger** |
| motion (mini build) | ~2.6–5.5 KB | **unverified** — every `mini/dist/*` path 404'd | — | remains an estimate, now labelled as one |
| lenis | ~3.5 KB gz | **5.4 KB gz** | 4.5% | rejection unchanged — it rests on R4's behavioural grounds, not the bytes |
| three | not considered | **419.7 KB gz** unminified shipped; **~213.4 KB gz** measured inside Google's Closure-compiled production bundle | **178%** at the practical anchor | rules option C out (§11.5.3) |

**The argument in §11.2 survives every estimate being wrong in the unfavourable
direction.** That is the useful property of having labelled them estimates.

#### 11.5.3 The "3D cube assembles on scroll" request — priced, and declined

A stakeholder asked for the effect on `codewiki.google`, where a 3D cube assembles
as you scroll. Full analysis in `MOTION-3D-ANALYSIS.md` §3–§6. The three options,
measured:

| | A: CSS 3D | B: hand WebGL | C: three.js |
|---|---|---|---|
| Dependency | **0 KB** | 0 KB | **150–250 KB gz** |
| Share of the 120 KB budget | 0% | 0% | **125–210%** |
| rAF loop | **none** | continuous | continuous |
| GPU memory | **20–25 MB** (6 render surfaces) | full-viewport target | full-viewport target |
| Can be the LCP element | **no** | no | no |
| Scroll-jank risk on low-end phones | **real** | high | high |

**What the cube actually is** (`MOTION-3D-ANALYSIS.md` §3.2, measured): **three.js
/ WebGL**, bundled — `WebGLRenderer` ×47, `PerspectiveCamera` ×10, `ShaderMaterial`
×29, `PMREM` ×11. `preserve-3d`, `matrix3d`, `rotate3d`, `animation-timeline` and
`view-timeline` all return **zero** across both bundles and the stylesheet. It is
**not** CSS 3D. Google's own page spends **504.6 KB gz of JavaScript** and
preloads the **213.4 KB gz** three.js bundle on its landing page.

**Verdict: not built.** Option A fits the byte budget but fails three of our own
constraints — it cannot be the LCP element, it therefore cannot be the home page's
above-the-fold accent, and its cost centre is GPU memory and scroll jank on the
devices §10.1 cares about, which no byte budget here tracks. Option C exceeds the
budget by 1.25×–2.1× on measured evidence. Option B is worse than C.

**The cheaper alternative** (`MOTION-3D-ANALYSIS.md` §5.6, option D): the same six
elements as a **flat grid** revealing in sequence on staggered `view()` ranges,
`translateY` capped at **12px** — inside §3's existing 6% / 24px ceiling. **0 KB.
Identical on mobile. No `preserve-3d`, so no persistent 3D render surface.** The
reduced-motion end state is the whole grid at `opacity: 1`, reached by the
`no-preference` gate rather than a fallback branch. **What it gives up is depth,
and that is a genuine downgrade** — it delivers "assembles in order as you read",
not "assembles as a solid".

**This does not change §11's total, which remains 0 KB.** It was the right answer
before the cube was priced and it is the right answer after.

#### 11.5.4 Open decision for the user

The cube would displace the home page's existing accent moment (the three section
reveals in §3.1) and would breach P2's one-accent-per-page rule. Three options are
set out in `MOTION-3D-ANALYSIS.md` §6. **`UI-UX-SPEC.md` §3.1 and the
`STITCH-PROMPTS.md` 3.1 block are unchanged pending the user's choice** — the
current three-section reveal remains the contract.

---

## 12. Backend gaps

Pages needing something the backend does not provide — **and, unusually, the pages
needing something the backend already provides and the storefront has not caught
up with.** Record only. Do **not** work around any of it in the client, and do
**not** implement a second copy of business logic. Money, stock, discount, coupon
and order state are server-side and authoritative.

Conventions: **P0** blocks a page from shipping at all; **P1** blocks a feature
the spec requires but has a documented degraded mode; **P2** is a known
limitation recorded so it is not rediscovered.

### 12.A Open questions — the backend HAS this, the decision is not mine

| # | Pri | Page | What is missing | UX it blocks | Proposed contract | Current workaround |
|---|---|---|---|---|---|---|
| **G1** | **P0** | `/checkout`, `/orders/[id]` | `POST /api/orders/checkout/` is `AllowAny` and returns `guest_token` **once** (`orders/views.py:289`, `:1067`). There is no decided policy for **where the client stores that credential** — `sessionStorage`, `localStorage`, a cookie, or not at all. It is the guest's only key to their own order and their only key to tracking | Guest checkout (CHANGE-11) cannot ship without it. Without persistence, a guest who closes the tab has an order they can never see again | **This is a decision, not an endpoint.** Proposing the shape, not the answer: a `__Host-`-prefixed `Secure; HttpOnly; SameSite=Lax` cookie scoped to `/orders` + `/shipping`, set by the frontend only if the backend returns it in a header-riding envelope — **or**, since the backend already has a CSRF-cookie mechanism, mint it server-side at checkout. **Escalate to the user and the backend owner.** Do not implement client-side storage of a bearer credential on a design decision | None. The current UI has no guest flow at all, so nothing regresses — but the "Sign in to check out" wall (CHANGE-11) blocks a supported path |
| **G2** | **P1** | `/orders/[id]`, `/track-order` | The tracking endpoint is public with the token as the credential, but **nothing ever emails the guest a tracking link**, and there is no `?token=` deep link in the confirmation email | A guest who did not copy the token cannot track. The `/track-order` form asks for a credential most guests will not have | Proposal only: the `ORDER_PAID` notification (§19.1) includes the `order_number`, and a guest confirmation would additionally carry a link to `/track-order?order_number=…&token=…`. **The token must not appear in a URL that gets logged by a proxy or a referrer** — `Referrer-Policy: strict-origin-when-cross-origin` is already set, but the query string itself is the risk, so a fragment (`#token=`) is safer | `/track-order` accepts a pasted token. Honest but low-friction-free |
| **G3** | **P1** | `/checkout` | `GET /api/shipping/estimate/` is `AllowAny` and needs `state` **and** `pincode` — both are required, and the view's docstring is explicit that resolving with only one could wrongly declare a destination unserviceable | Any delivery estimate before the address step. Spec §3.7's mock shows shipping as "Calculated", so the customer expects a price before payment | None needed. The estimate is called on blur of pincode+state. **The gap is that the cart page cannot show delivery at all** (no destination exists yet), which is correct — but it means the cart's "Final totals are confirmed at checkout" line is the only honest thing it can say | Cart shows an estimated subtotal and says the total is confirmed at checkout (KEEP) |

### 12.B Genuine backend gaps — nothing exists

| # | Pri | Page | What is missing | UX it blocks | Proposed contract | Current workaround |
|---|---|---|---|---|---|---|
| **G4** | **P1** | `/orders`, `/orders/[id]` | `src/lib/types.ts` `Order`/`OrderItem` are missing **13 fields the serializer already sends** (`order_number`, `payment_status`, `fulfilment_status`, `payment_method`, `shipping_method`, `shipping_amount`, `shipping_method_code`, `currency`, and six lifecycle timestamps; `OrderItem` lacks `sku`/`variant_name`/`currency`) | The lifecycle timeline (§5.5) cannot be typed; `order_number` cannot be displayed | **No backend change needed** — this is a frontend type correction against `orders/serializers.py:83`. Recorded here because the consequence (a stale client type silently dropping a server field) is exactly the class of defect §12 exists to catch | The fields are simply not rendered |
| **G5** | **P2** | `/cart` | The cart coupon is previewed via `POST /api/orders/apply-coupon/` and the code parked in `sessionStorage` (`CouponForm.tsx:11`). `POST`/`DELETE /api/cart/coupon/` exists and persists the coupon on the cart row | A refresh loses an applied coupon, and the code must be re-sent as `coupon_code` at checkout. Both endpoints are already in `config/urls.py` | **No backend change needed.** A frontend switch to the persisted endpoint. Recorded because `apply-coupon` remains the right *preview* call and the two should not be confused | `sessionStorage` mirror; coupon re-validated at checkout server-side |
| **G6** | **P1** | `/products/[slug]` | `POST /api/products/{slug}/restock-notifications/` exists (SPEC-19-4, throttle scope `restock` at 5/min) and the storefront does not call it | The sold-out PDP is a dead end. This is the real functional replacement for the "customers also bought" carousel the design bans | **No backend change needed.** A frontend addition | The PDP shows "Sold out" and nothing else |
| **G7** | **P0** | `/orders/[id]` | `BACKEND_REQUESTS.md`'s P0 "no webhook" entry is **stale** — `/api/v1/webhooks/razorpay/` exists. What is still missing is the *client's* ability to tell a **captured-but-409'd** order from a **never-paid** one: both are `pending` in `GET /api/orders/` | The right message on the pending order. Today the UI offers "Retry payment" to an order whose money may already have been taken — the very double-charge risk `PaymentLauncher`'s `maybe-processed` phase exists to prevent | Proposal only: a boolean on the order read (e.g. `payment_attempted`), or a dedicated `GET /api/orders/{id}/payment-state/`. **Marked as a proposal; the backend is the source of truth for money state** | `PaymentLauncher`'s `maybe-processed` branch handles the 400-already-processed case at verify time. It cannot help before the customer clicks pay |
| **G8** | **P0** | **`/checkout` — every route, in fact** | **`frontend/next.config.ts` sets `script-src 'self' 'unsafe-inline'` and `frame-src` is absent. `src/lib/razorpay.ts:19` injects `https://checkout.razorpay.com/v1/checkout.js`. The CSP blocks it.** This is a frontend-config defect, not a backend gap, recorded here because it is a hard blocker and because §12 is the record-only table | **The payment step cannot load the gateway.** Not "may fail" — cannot | Requires `script-src https://checkout.razorpay.com`, plus `frame-src`/`child-src` for the gateway's iframes and `connect-src` for its API. **That is a materially weaker CSP and it is a decision for the user and the backend owner, not for a design spec.** The alternative — a same-origin proxy for the gateway script — is worse and is not proposed | **None.** Record-only. Do not work around it in the client by relaxing the check, by moving the script tag, or by `eval`. §9.3 says the payment e2e cannot be written until this is fixed, and that is deliberate |
| **G9** | **P2** | `/products`, `/products/[slug]`, `/` | **No reviews read endpoint.** Spec §6.11 has reviews for *moderation*; there is no public read | Star ratings, review counts, testimonial sections. **This is why every reference site's most prominent feature is absent here** — and it is an absence with a reason, not a restraint | Proposal only, and it is a real project: a paginated public read with an aggregate rating, plus a write path. Out of scope for this redesign | None. No rating is rendered anywhere |
| **G10** | **P2** | `/products/[slug]` | `Product.description` is one free-text field. There is **no structured note data** (top/heart/base), no `images` relation, no `compare_at_price`, no `in_stock` (`products/serializers.py:14` is a pinned whitelist — the backend's own tests assert it) | The three-column notes layout, the size/variant selector, the gallery, strikethrough pricing, an explicit availability flag | Proposal only, all of it backend: a `ProductNote` relation or a JSON field; `Product.images`; `in_stock: bool` (already P2 in `BACKEND_REQUESTS.md`) | Single image + static size line + prose description + `stock > 0` derivation. Each is what the data permits |
| **G11** | **P2** | `/products/[slug]`, `/shipping`, `/returns` | `GET /api/settings/` returns **only** `support_email`, `support_phone`, `whatsapp_number`, `whatsapp_message`, `instagram_url` (`ops/views.py` `api_settings`). There is **no return window, no dispatch window, no shipping policy text** | The PDP's delivery-and-returns line and the scannable facts at the top of `/shipping` and `/returns` are **hardcoded copy** that must be hand-edited when the policy changes | Proposal only: extend the same public settings reader with `return_window_days`, `dispatch_window_days`, `shipping_policy_url`. Same cache semantics (short revalidate), same admin-editable singleton | Static copy in the page sources. **Recorded because hardcoded policy copy is a real liability**: the 7-day window appears in three places (`page.tsx:105`, `returns/page.tsx`, `privacy`/`terms`) and can drift |
| **G12** | **P2** | `/track-order` | No email carries a tracking deep link (see G2) | Guest tracking is friction-heavy without it | See G2 | Pasted token |
| **G13** | **P1** | `/returns` | **No customer-facing returns endpoint.** The only returns machinery is `POST /api/admin/orders/{id}/refund/`, staff-gated behind `refund` capability | The coverage contract calls `/returns` "Return request and status". It is a **policy page**, and the honest design keeps it that way | Proposal only, and it is substantial: a customer return request (order-scoped, eligibility checked against the same policy the page states, staff-inbox-routed). Also needs an `eligible_until` field on the order so the page's stated window and the backend's enforced window cannot diverge | A `mailto:` with a pre-filled subject, naming the order number. **Shipping a form that posts nowhere would be the worst available outcome** — it would collect an address and a reason and discard both |
| **G14** | **P2** | `/products`, `/products/[slug]` | No `in_stock` boolean; the storefront derives `stock > 0` | The UI can neither avoid leaking exact stock counts nor avoid implying inventory precision the backend does not manage (there are no reservations) | See G10 | `stock > 0`, and the numeric stock is **never displayed** |
| **G15** | **P2** | `/products/[slug]` | No slug-redirect mechanism (spec §18.1 "appropriate redirects for changed slugs") | A renamed product 404s | Proposal only: a `ProductRedirect` table with a lookup on the detail view, or a Django `Redirect`-style middleware | `notFound()` renders the branded 404, which now has four real links (CHANGE, §5) |
| **G16** | **P2** | `/` , `/contact` | No homepage-editor or page-content endpoint (spec §6.10 is admin-side), no newsletter-subscriber write (spec §3.2 §09), no contact-form write | Spec §3.2's recommended homepage sections 03 (shop by category), 08 (reviews) and 09 (newsletter). **None ships** | All backend. Each is a small table plus a public write | **The sections are omitted with a stated reason** (§5.1), not silently. That is the difference between a scope decision and a bug |
| **G17** | **P2** | every route | `GET /api/accounts/me/` still does not exist (`lib/tokens.ts:99` says so, and it is still true) | Identity display in the header; a server-side source for a repeat customer's address | See `BACKEND_REQUESTS.md` P1 | `localStorage` address prefill with the full PII treatment (KEEP VERBATIM) — same-browser only, disclosed at the point of prefill, forgetable, cleared on logout |
| **G18** | **P1** | `/checkout` | The **session cart has no user binding** (`Cart.user` does not exist; see `BACKEND_REQUESTS.md` P1). Checkout requires the *same session cookie* that built the cart **plus** the JWT | A cart on a new device, or after a session expiry, silently disappears; checkout then 404s | Backend. Not a design problem, but the reason `/cart`'s copy must keep the specific "Your cart session expired. Please add items again." message (KEEP) rather than a generic error | The 404-specific copy, which is already correct |
| **G19** | **P2** | `/api/v1/` migration | The backend dual-mounts every customer route under `/api/v1/store/…` and `/api/v1/account/…` (`config/urls.py`), with legacy `/api/…` as aliases, and says the base-URL cutover "is coordinated separately (S9 ledger note)" | Nothing visible. Recorded because the redesign touches every API call site and the cutover should not be bundled into a UI/UX promotion | Backend-led. **Recommendation: do NOT bundle it.** One change per PR | The frontend uses the legacy `/api/…` family throughout (`lib/*-api.ts`) |

### 12.C What this table is not

None of the above is worked around in the client. Specifically, and
non-negotiably:

- **No client-side money math beyond display arithmetic.** `formatINR` formats;
  `CartView`'s subtotal is labelled an estimate; the checkout delivery line comes
  from the server; `Order.total_amount` is the only figure shown once an order
  exists. **No coupon is evaluated on the client.** No shipping rate is computed
  on the client.
- **No client-side stock truth.** The stepper's `max = stock` is a courtesy; the
  backend's "Not enough stock" 400 is the answer, and `CartLineItem` already
  reconciles against it (KEEP).
- **No client-side order state machine.** The six phases of `PaymentLauncher` are
  the backend's states rendered. The client does not decide what `pending` means.
- **No enumeration oracles.** `EmailOnlyForm`, `LoginForm` and the new
  `ShipmentTimeline` render the backend's uniform answers verbatim and never
  branch on them. `conventions.md:25` requires this and the backend's audit cycles
  spent real effort closing it.

---

## 13. Assumptions

Every claim in this spec that could not be verified from source, listed. An
unstated guess is a defect in this deliverable.

Each is labelled with what I did instead of knowing, and what would falsify it.

### 13.1 Verified, and how

Everything in the KEEP/CHANGE/DROP audit, every route claim, every endpoint, the
CSP contents, the palette, `next/font` config, `package.json`, the
`PRODUCTS_PAGE_SIZE` value, the serializer field lists, the
`@permission_classes([AllowAny])` on `create_order`, the tracking endpoint's
credential model and uniform-miss behaviour, the coupon endpoints, the restock
endpoint, the webhook mount, the `AllowAny` + required-both-fields rule on the
shipping estimate. All read from source on 2026-10-02 in the UI worktree at
`df84f19`.

**The contrast ratios are computed, not estimated** — the WCAG relative
luminance formula applied to the literal hex values in `globals.css`.

### 13.2 Assumptions

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| **A-1** | **"Maison Aurel" is the real brand** and the wordmark/tagline may be restyled but not renamed | `lib/site.ts` is the single source and is env-overridable; nothing in the brief names a brand | Cosmetic only. Every token and layout decision is brand-agnostic |
| **A-2** | **`Fraunces` and `Source Sans 3` remain licensed and fetchable at build time.** `next/font/google` downloads them during `next build`, so **an offline build runner fails** | `next/font/google` behaviour, and `PLAN.md` §6's commitment to "zero third-party font requests" (runtime, not build time) | The build fails, loudly. This is pre-existing, not introduced. A `next/font/local` vendoring step is the fix and is out of scope |
| **A-3** | **Target browser floor is Chrome/Edge 115+, Safari 26+.** `animation-timeline` is Chrome 115+ and Safari 26+; **Firefox is not assumed to support it** | Feature availability at time of writing | Firefox gets the fully static, fully correct page — which is why every scroll-driven rule is behind `no-preference` rather than `@supports`. **No design decision depends on this being right**; if Firefox ships it, the animations appear and the page gets better |
| **A-4** | **The 120 KB first-load JS budget is a committed figure, not a measured one.** I did not build the app or measure a bundle | `PLAN.md` §6 states it as a target; no CI gate enforces it (§13.1 of `PLAN.md` lists no budget script) | If the real baseline is already above 120 KB, the §10.2 per-route figures are wrong and §11.4's "GSAP is 26% of the budget" argument is overstated. **Measure before quoting.** The *relative* argument (a library is a large fraction of a small budget) survives either way |
| **A-5** | **Font sizes (35–55 KB gz each variable latin subset) are estimates** from general knowledge of variable-font sizes | Not measured | §10.4's total moves; the "do not add a third family" conclusion does not |
| **A-6** | **SUPERSEDED 2026-10-03 by measurement.** ~~Published sizes for GSAP, `motion`, Lenis are order-of-magnitude figures.~~ Now **measured**: gsap 28.3 KB gz, ScrollTrigger 18.0 KB gz (46.3 combined), motion 46.7 KB gz, lenis 5.4 KB gz; three.js 419.7 KB gz unminified with a **213.4 KB gz** measured anchor from Google's Closure-compiled production bundle. Method and caveats in `MOTION-3D-ANALYSIS.md` §0 and §4. **One residue:** the `motion/mini` figure could not be verified — every `mini/dist/*` path returned 404 — so the old "~2.6–5.5 KB" remains an **unverified estimate** and is labelled as one | Every figure in §11.3 moved **against** the rejection, which therefore strengthens. The relative argument (a library is a large fraction of a small budget) survives either way |
| **A-7** | **The design-system and per-page Stitch prompts have not been run.** This document is written from source and from the reference sites; the prompts in `STITCH-PROMPTS.md` are unexecuted | Human-in-the-loop by design; no Stitch API and no MCP | They will need iteration. §4 of that file is the place to record what changed. Nothing in this spec depends on a Stitch output existing — the spec is executable from words alone, which is the stated contract |
| **A-17** | **REVISED 2026-10-03.** ~~No Lighthouse, axe or bundle-size run was performed.~~ Now: **seven third-party sites measured by HTTP transfer size** (HTML bytes, script-count, `<script src>` gzip transfer sums, library fingerprints, `prefers-reduced-motion` presence) — `MOTION-3D-ANALYSIS.md` §2. **Still true and still open: no build of our own app, no Lighthouse, no axe, no bundle measurement.** Every §10 target remains a target | §10 is a specification, not a measurement. The seven site figures are **comparative only** — one machine, one network, cold cache, transfer size is not "first-load JS", and a Shopify script sum includes popups and chatbots this storefront would not ship. `MOTION-3D-ANALYSIS.md` §0.1 states each limit |
| **A-18** | *(new, 2026-10-03)* Site figures are **cold-cache first load** with a desktop Chrome UA | Shopify serves bots and users the same document | A returning visitor with a warm HTTP cache transfers far less. Direction of every comparison is unchanged |
| **A-19** | *(new, 2026-10-03)* The CSS-3D cube GPU-memory figure assumes 640–800 px effective face size | Used only for the `MOTION-3D-ANALYSIS.md` §5.1 arithmetic | At 400 px it halves; above 1000 px it doubles. The recommendation is robust across 400–800 px because it rests primarily on the **LCP-eligibility** and **six-element-budget** arguments, which are hardware-independent |
| **A-20** | *(new, 2026-10-03)* Same-origin bundling of npm dependencies (`/_next/static/chunks/*.js`) is Next's documented behaviour, **not observed from our build** — there is no `node_modules` and no `next build` was run | `next.config.ts` sets `script-src 'self'`; the §11.5.1 retraction depends on this | If a dependency somehow loaded cross-origin, §11.5.1 would be wrong. **One command confirms it:** `npm run build` then inspect `_next/static/chunks`. It does not change §11's total, which is 0 KB either way |
| **A-21** | *(new, 2026-10-03)* three.js will not tree-shake below ~150 KB gz in a production build | Anchored by the measured 213.4 KB Closure-compiled full-library bundle | If a minimal scene tree-shook to 60 KB, §11.5.3's rejection of option C would need re-arguing. No evidence for that, and §5.5's recommendation is a "no" that also rests on LCP eligibility and layer count |
| **A-22** | *(new, 2026-10-03)* **The throughput figures behind §11.4.1 are a mix of measurement and model.** `MOTION-3D-ANALYSIS.md` §11.1 **measured** a 286,751 B gz payload at 0.166–0.193 s unthrottled, 1.164–1.166 s under a 200 KB/s cap and 5.157–5.177 s under a 50 KB/s cap, with ±0.2–0.4% repeat variance | The **caps are model constants**, not measured mobile conditions — A-23 in the analysis file. The 218× spread between "this machine" and "400 Kbps" is a consequence of the caps being chosen | If real users sit at 1.6 Mbps rather than 400 Kbps, the penalty for doubling the budget is **+0.60 s rather than +2.40 s**. The *ratio* is more trustworthy than either absolute |
| **A-23** | *(new, 2026-10-03)* **The ~250-line / 5-file / 6-task pivot estimate in §11.4.1 is an order-of-magnitude estimate, not a measurement.** No build exists and no builder ran | Derived from §3's motion table (only ~6 rows are scroll-driven) and §6's inventory | The **shape** is defensible from the architecture the spec mandates — token layer, token-layer reduced motion, P2. The exact line count is not. A-28 in the analysis file |
| **A-24** | *(new, 2026-10-03)* **`motion`'s `mini` entry cannot deliver scroll-linked work**, which is why tier (b) in §11.4.1 is priced at the measured 46.7 KB full build rather than the old "~2.6–5.5 KB" estimate | `mini` is documented as animate-only; I did not retrieve that documentation | **The assumption most likely to be wrong, and worth checking** — it is the only place a materially cheaper option might exist. If `mini` does include scroll timelines, tier (b) drops sharply and §13's curve in the analysis file gains a low-cost scrub option |
| **A-25** | *(new, 2026-10-03)* **The LCP and hydration figures in §11.4.1 are models.** LCP "up to ~1.2 s" is bandwidth-contention arithmetic; "+0.6–1.2 s to interactive" uses a widely-cited ~1 ms parse+compile per 100–200 KB on a mid-range phone | Neither measured on a real page — our LCP element is an image, so the effect is contention, not sequencing | The weakest numbers in this round. **§11.4.1's conclusion does not depend on them**: the +2.40 s bandwidth figure carries the argument on its own |
| **A-8** | **`/api/v1/` cutover will not be bundled into this redesign** (§12-G19) | The backend explicitly coordinates it separately ("S9 ledger note") | If it is bundled anyway, the per-route JS figures are unaffected but the diff is much larger and the audit surface grows. Recommend against |
| **A-9** | **The backend floor in §14.6 (1757 tests / 1753 pass + 4 expectedFailure / 100.00%) may be stale.** The most recent ledger entries I read mention a floor rising 1515 → 1617 | Two different figures in the repo's own history; **§14 is not mine to edit** (§14 preamble) | §14.6's comparison would fail spuriously. **I have deliberately not touched §14.** Flagged here so the orchestrator can reconcile the number before running the checklist, since §14.8 makes a backend test regression a stop condition |
| **A-10** | **`spec-ui` is currently 1 commit ahead of `origin/feat/add-frontend` with no drift** (merge-base `4e424fb` == the `origin` tip) | `git merge-base` / `git rev-list`, read on 2026-10-02 | §14.4's merge will produce conflicts rather than a fast-forward. The checklist anticipates this (§14.5) |
| **A-11** | **`GET /api/orders/{id}/` is owner-scoped and safe to use from the detail page** | `orders/urls.py` mounts `order_detail`; `BACKEND_REQUESTS.md` P2 describes it as owner-scoped | If it is not owner-scoped, switching from list-and-find to it would be a **security regression**. **Verify before implementing CHANGE-13.** I read the urlconf and the serializer, not the full view body for `order_detail` |
| **A-12** | **Guest checkout's `guest_token` is not sensitive beyond "this is my order".** It is 32 bytes of `secrets.token_urlsafe` (`orders/views.py:134`) | Source | It is a bearer credential for a PII-bearing read (`guest_email`, address, phone). §12-G1 treats it as a credential requiring a storage decision, which is the conservative reading and the one I would want reviewed |
| **A-13** | **`spec §3.2`'s ten-section homepage is a recommendation, not a requirement.** Six of its sections (category tiles, featured/best-selling, reviews, newsletter, promo collection) have no data behind them | The spec's own section 15.1 says the palette is "illustrative"; §3.2 is headed "Recommended section order" | If the client wants all ten, the missing endpoints are §12-G16 and it becomes a backend project, not a design one. Recording the omission with a reason is the honest reading; §5.1 states it explicitly rather than dropping them silently |
| **A-14** | **`/returns` stays a policy page** despite the coverage contract's "Return request and status" one-liner | There is no customer-facing returns endpoint (§12-G13) | If a returns endpoint lands, §5.7 gains a step and a form. The design is written so that addition is additive |
| **A-15** | **`SearchAction` uses `?search=`, not `?q=`.** §8.4 proposes it as a judgement call | `filters.ts` and `products/views.py:37` both use `search` | If `q` is not accepted, the JSON-LD is not a valid `SearchAction`. **Recommendation: omit `SearchAction` entirely rather than emit a non-functional one.** Google has deprecated the `q` autocomplete parameter anyway |
| **A-16** | **The e2e helpers are genuinely broken, not merely stale-looking** (§9.2) | `lib/tokens.ts` stores no `aurel.refresh`; `accounts/urls.py` documents `login/` as the staff door | If some middleware still injects that key, the helpers work. **I did not run the suite** (no frontend `node_modules`, and running the app requires a live backend). The fix is small either way and the specs must move with the redesign regardless |
| ~~A-17~~ | **Superseded and merged** into A-17 above (2026-10-03). The original "no Lighthouse, axe or bundle-size run was performed" note is retained there in struck form; **the part that remains true is that *our own app* has still never been built, bundled or measured.** `MOTION-3D-ANALYSIS.md` §7.4 lists exactly what is still unverified |

---

## 14. RECONCILIATION CHECKLIST — run by the BACKEND orchestrator

> **Load-bearing.** The backend orchestrator runs this verbatim, in the UI
> worktree, at integration time. It is written as ordered executable steps. Stop
> on any condition in §14.8.
>
> **Do not edit this section without the user's say-so.** The backend
> orchestrator executes it; if it is wrong, both runs are wrong.

### 14.1 Preconditions

```
Backend orchestrator:  worktree  C:\Users\Admin\AppData\Local\Temp\opencode\spec-comp-wt
                       branch    spec-comp            (NEVER switch this)
UI orchestrator:       worktree  C:\Users\Admin\AppData\Local\Temp\opencode\ui-wt
                       branch    spec-ui              (NEVER switch the other)
Shared target:         feat/add-frontend           (written ONLY by a release-engineer gate)
```

Both orchestrators must be **idle**. No builder, no auditor, no gate running.

### 14.2 Verify both sides are clean and complete

```
# in spec-comp-wt
git status --porcelain          # MUST be empty
git log --oneline -1

# in ui-wt
git status --porcelain          # MUST be empty
git log --oneline -1
```

Also confirm the backend queue is CLOSED — i.e. the section-1 ledger shows no task
in `IN-AUDIT`, `BUGS-FOUND`, or `IN-COMPLIANCE`, and the end-of-run sweep has run.
**Do not integrate into an open backend queue.** A moving backend API means UI work
built against it is work that must be redone.

### 14.3 Confirm the UI work is audited

`spec-ui` carries un-audited code until the `auditor` has returned SHIP on it. The
auditor's floor is measured against `frontend/package.json` scripts
(`npm run test`, `npm run build`) — animation work that passes a visual glance can
still fail reduced-motion, focus, or a11y. **Do not merge un-audited UI.**

### 14.4 Bring the backend up to date inside the UI worktree

```
# in ui-wt
git fetch origin
git merge origin/feat/add-frontend
```

`spec-ui` was branched from `feat/add-frontend` and will have drifted behind as
backend trains promoted. Merge, do not rebase — this branch is shared state and a
rebase rewrites published history.

### 14.5 Resolve conflicts backend-first

If the merge conflicts, the **backend side wins by default**, because the backend
is the source of truth for money, stock and order state. Where a conflict touches
a component that both sides changed, reconcile by hand and record the decision —
never take "ours" or "theirs" wholesale on a conflict file.

### 14.6 Re-run both suites in the UI worktree

```
# backend, from ui-wt\backend, using the Downloads venv (the worktree has none):
& "C:\Users\Admin\Downloads\backend\backend\venv\Scripts\python.exe" -m coverage run manage.py test
& "C:\Users\Admin\Downloads\backend\backend\venv\Scripts\python.exe" -m coverage report

# frontend, from ui-wt\frontend:
npm run test
npm run build
```

Backend floor must not regress (last recorded: **1757 tests / 1753 pass + 4
expectedFailure / 100.00% coverage**). A merge that drops a passing test or a
migration-drift check is a **stop condition**.

### 14.7 Promote

```
# in ui-wt — the release-engineer only, never the orchestrator
git push origin spec-ui
gh pr create --base feat/add-frontend --head spec-ui --title "Promotion (UI/UX redesign)"
gh pr checks <n> --watch --interval 30
```

CI must be all-green including gitleaks and GitGuardian before merge. Label the PR
as the UI redesign promotion, not a spec-compliance train.

### 14.8 STOP CONDITIONS — halt and escalate to the user

1. Any backend test regression or coverage drop after the merge.
2. `makemigrations --check` reporting drift after the merge.
3. A conflict touching a money, stock, order-state or payment path.
4. gitleaks or GitGuardian red on the promotion PR.
5. The UI branch not carrying an auditor SHIP.
6. `origin/master` observed to have changed — it is frozen and must not.
7. Any file appearing in both diffs that was not reconciled deliberately.

### 14.9 After a clean merge

Record the promotion PR number, merge SHA and the branch tip in
`backend/docs/spec-run-state.md`, and confirm `origin/feat/add-frontend` now
carries both the backend queue and the UI redesign. Neither orchestrator may
delete `spec-ui` or `spec-comp` — they are the audit trail.
