# TEMPLATE.md — the white-label contract

This document is the contract for rebranding and redeploying this storefront
as a different merchant's shop. Everything here is derived from the code
paths named below; where the code does not yet support a knob, this document
says so instead of describing one.

Paths are relative to `frontend/`.

---

## 1. What this template is

- **One Next.js (App Router) application**, server-rendered, TypeScript,
  Tailwind v4. All pages, components and styles ship in one deployable unit.
- **The backend is a separate headless API** (Django). The storefront talks to
  it through `NEXT_PUBLIC_API_BASE_URL`: read in `src/lib/config.ts`
  (`API_BASE`) for client fetches, server-side fetches
  (`src/lib/server-settings.ts`) and media URL resolution, and read directly
  from the environment by `next.config.mjs` at build time to derive the
  CSP/image allowlists.
- **One env file per merchant** (`.env.local`, copied from
  `.env.local.example`) selects the API origin, the canonical site URL, the
  brand strings and the contact channels. `NEXT_PUBLIC_*` values are baked in
  at build time: changing them requires a rebuild.
- **The backend owns all commerce truth**: prices, stock, coupons, discounts,
  tax and order totals are computed server-side (see §6).
- **Static routes are registered** in `src/lib/pages.ts`; the sitemap and
  robots files consume that registry, so SEO metadata cannot drift per page
  (see §3).

---

## 2. The config seams

| Seam | File | Controls | How a merchant changes it |
| --- | --- | --- | --- |
| Template manifest | `src/lib/template.config.ts` | brand, contact, nav/footer links, feature flags | edit the file, or set `NEXT_PUBLIC_*` env for brand/contact (rebuild required) |
| Theme seed | `src/lib/theme.ts` | four token slots (two accent colours, two font stacks) | **one-file edit** of `DEFAULT_SEED` — no env knob exists (see below) |
| Env vars | `.env.local` (from `.env.local.example`) | API origin, site URL, brand/contact overrides, admin redirect | edit + rebuild |
| Contact settings | Django admin → `/api/settings/` | contact channels at runtime | Django admin Site settings; no rebuild (60 s server cache) |

### 2a. `template.config.ts` — the manifest

`TemplateManifest` has five groups:

- **`brand`** — `name`, `tagline`, `description`, `url`. Each falls back to a
  default when its `NEXT_PUBLIC_*` var is unset or blank.
- **`contact`** — `supportEmail`, `supportPhone`, `whatsappNumber`,
  `whatsappMessage`, `instagramUrl`. A channel that resolves to empty is
  hidden in the UI (the contact components render it only when it has a
  value). Contact channels resolve in priority order **Django admin Site
  settings → build-time env → hidden**, implemented in
  `src/lib/server-settings.ts`; if the API is unreachable, the env fallbacks
  keep the footer rendering.
- **`nav`** and **`footer`** (`shop` / `help` / `legal`) — link + label
  manifests, consumed by the components themselves: `Header.tsx` renders
  `templateConfig.nav`, `Footer.tsx` renders the three footer groups, and
  `MenuOverlay.tsx` derives the mobile menu from `footer.shop` minus `/login`
  (see §7).
- **`featureFlags`** — see §4.

`src/lib/site.ts` re-derives the flat `site` export from this manifest;
every import of `site` is a public contract and must keep compiling. Brand
strings flow from there into page metadata, JSON-LD, header/footer copy and
the legal/contact pages.

### 2b. Theme seed → token slots (`src/lib/theme.ts`)

The seed maps onto **existing** custom properties in `src/app/globals.css`;
it never introduces new tokens and never edits the stylesheet:

| Seed field | Token slot | Default (canonical) |
| --- | --- | --- |
| `brand` | `--color-bronze` | `#8c5338` |
| `accent` | `--color-bronze-alt` | `#885035` |
| `displayFont` | `--font-display` | playfair-display stack |
| `bodyFont` | `--font-sans` | plus-jakarta-sans stack |

`layout.tsx` applies `seedToCssVars(DEFAULT_SEED)` as an inline style on the
root element, so the default seed is a zero visual delta.

**Honest state:** there is **no env wiring for the seed** — no
`NEXT_PUBLIC_BRAND_COLOR`-style knob exists. The supported change today is a
one-file edit of `DEFAULT_SEED` in `src/lib/theme.ts`, followed by a rebuild.
Only these four slots are seedable; derived colours (focus ring,
danger/success, the ink family) stay stylesheet-owned because they are
contrast-audited as a set. Fonts are restricted to the four-key allowlist in
that file — no new webfonts may be loaded.

### 2c. `NEXT_PUBLIC_*` environment variables

From `.env.local.example`, as read by `template.config.ts`, `site.ts` and
`config.ts`:

| Variable | Read by | Effect | Default if unset |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `src/lib/config.ts`; `next.config.mjs` | API origin for all fetches, media URLs, CSP `connect-src`/`img-src`, `images.remotePatterns` | `http://127.0.0.1:8000` |
| `NEXT_PUBLIC_SITE_URL` | `template.config.ts` → `site.url` | canonical origin for metadata, sitemap, robots | `http://localhost:3000` |
| `NEXT_PUBLIC_SITE_NAME` | `template.config.ts` → `site.name` | brand name everywhere | template default brand |
| `NEXT_PUBLIC_SITE_TAGLINE` | → `site.tagline` | title tagline | template default |
| `NEXT_PUBLIC_SITE_DESCRIPTION` | → `site.description` | meta descriptions | template default |
| `NEXT_PUBLIC_SUPPORT_EMAIL` | → `site.supportEmail` | support inbox (also SMTP sender) | template default inbox |
| `NEXT_PUBLIC_SUPPORT_PHONE` | → `site.supportPhone` | displayed phone; empty = hidden | empty (hidden) |
| `NEXT_PUBLIC_WHATSAPP_NUMBER` | → `site.whatsappNumber` | `wa.me` deep link; empty = hidden | empty (hidden) |
| `NEXT_PUBLIC_WHATSAPP_MESSAGE` | → `site.whatsappMessage` | pre-filled WhatsApp text | default question string |
| `NEXT_PUBLIC_INSTAGRAM_URL` | → `site.instagramUrl` | profile link; empty = hidden | empty (hidden) |
| `NEXT_PUBLIC_ADMIN_ORIGIN` | `src/app/admin/page.tsx` | absolute origin of the Django admin; `/admin` 307-redirects to `<origin>/admin/` | `https://api.boredompeaks.me` — **no default**; the build fails if unset |

All `NEXT_PUBLIC_*` values are inlined at build time — after editing
`.env.local`, run `next build` again.

---

## 3. Add a page in 3 steps

1. **Create the route**: add `src/app/<path>/page.tsx` with its own
   `metadata` export (title + description).
2. **Register it** in `src/lib/pages.ts`: `path`, `title`, `description`,
   `indexable`, and a `navGroup`. `src/app/sitemap.ts` picks up indexable
   paths and `src/app/robots.ts` picks up the disallow list from this one
   registry — never hand-edit those two for a static route. Dynamic routes
   (`/products/[slug]`, `/orders/[id]`) are deliberately absent from the
   registry; they are resolved at request time.
3. **Surface it in navigation**: add the link to `nav` or the relevant
   `footer` group in `src/lib/template.config.ts`. **Current reality:** this
   is the only edit — `Header.tsx`, `MenuOverlay.tsx` and `Footer.tsx` all
   render from the manifest (the mobile menu derives from `footer.shop` minus
   `/login`), so a manifest link appears without touching a component (§7).

---

## 4. Feature flags

Declared in `templateConfig.featureFlags`:

| Flag | Intended gate (per flag name + manifest comment) | Default |
| --- | --- | --- |
| `guestCheckout` | checkout without a signed-in account | `true` |
| `restockNotifications` | back-in-stock notification flow | `true` |
| `orderTracking` | order tracking surface | `true` |
| `returnsRequests` | returns request flow | `true` |
| `reviews` | product reviews (off until a tenant curates moderation) | `false` |

**Honest state:** the flags are declared, typed and unit-tested for their
defaults and shape, and **two of the five have consumers**:

- `guestCheckout` gates the whole guest path: off = the `/checkout` sign-in
  wall as before (`CheckoutView.tsx`); on = an anonymous visitor gets the real
  flow and the backend's own 403 refusal becomes a sign-in hand-off at submit.
  It also picks the anonymous CTA copy in `CartView.tsx`.
- `restockNotifications` switches the sold-out PDP's restock form
  (`src/app/products/[slug]/page.tsx`, the flag is the kill switch).

The other three — `orderTracking`, `returnsRequests`, `reviews` — are read by
**no component**: flipping them changes nothing today. (`/track-order` exists
as a route but does not read `orderTracking`.) The defaults in the table stay
the contract the remaining wiring must honour.

---

## 5. Deploy

**Environment** (see §2c table and `.env.local.example`): at minimum
`NEXT_PUBLIC_API_BASE_URL` (same-site with the frontend origin — a
`127.0.0.1`/`localhost` split breaks the session cart cookie),
`NEXT_PUBLIC_SITE_URL`, the brand/contact overrides, and
`NEXT_PUBLIC_ADMIN_ORIGIN` (required — the build fails if it is unset).

**Scripts** (`package.json`):

| Script | Command |
| --- | --- |
| `npm run dev` | `next dev` |
| `npm run build` | `next build` |
| `npm run start` | `next start` |
| `npm run test` | `vitest run` |
| `npm run test:e2e` | `playwright test` |
| `npm run preview` | `opennextjs-cloudflare build` + `preview` |
| `npm run deploy` | `opennextjs-cloudflare build` + `deploy` |

**Deploy target files** (pointers only — consult them, they are the source of
truth):

- `open-next.config.ts` — OpenNext Cloudflare config (default factory).
- `wrangler.jsonc` — Worker name, assets/services/images bindings,
  compatibility flags, and the custom-domain route. The worker name and route
  pattern are per-merchant **file edits**, not env vars (§7).
- `next.config.mjs` — security headers; the CSP and image allowlist are
  derived from `NEXT_PUBLIC_API_BASE_URL` at build time (Razorpay hosts are
  allowlisted for checkout).

---

## 6. Hard rules

1. **No hardcoded brand, policy or currency values in components.** Brand and
   contact strings come from `site` / `templateConfig` (§2a); policy copy is
   merchant content owned by the legal pages; currency is not a literal in
   components. Known exceptions that still violate this today are listed in
   §7 — do not add more.
2. **Money renders from the API; no client-side money math.** Prices,
   subtotals, discounts, coupon and tax amounts and order totals are server
   responses. `src/lib/money.ts` is display-only formatting and never does
   arithmetic. The one sanctioned exception is the running cart/checkout
   subtotal, an optimistic display estimate over server-provided item prices
   that reconciles to the server's confirmed totals at checkout — never
   implement coupon, discount, tax or total rules on the client.
3. **Never invent numbers.** Any figure shown (price, delivery window, policy
   number) must come from the API or from merchant-approved copy. Every value
   the UI is required to take from the API is enumerated in
   `docs/design/UI-UX-SPEC.md` §12.D (API BINDING) — read that
   section by name before wiring a new data field; do not substitute a
   plausible-looking constant.

---

## 7. Honest limits — what still requires code edits today

- **Nav/footer manifest → components:** wired — `Header.tsx` renders
  `templateConfig.nav`, `Footer.tsx` renders the three `footer` groups, and
  `MenuOverlay.tsx` derives its list from `footer.shop` (minus `/login`).
  Editing the manifest alone now moves the UI; the one nuance is that the
  mobile menu follows the footer's shop group, not `nav`.
- **Three feature flags still have no consumers** (§4) — `orderTracking`,
  `returnsRequests` and `reviews` flip nothing today; `guestCheckout` and
  `restockNotifications` are wired.
- **Theme seed has no env knob** (§2b) — rebranding the accent/fonts means
  editing `src/lib/theme.ts` and rebuilding; only four slots exist, and
  derived colours/fonts are intentionally not seedable.
- **Currency/locale is fixed:** `money.ts` formats `en-IN` / `INR`. A
  different currency is a code change (then re-verify every money surface).
- **Policy copy is code:** legal/shipping/returns page prose (including
  region-specific wording such as India pincode and delivery-window text)
  lives in the page components; per-merchant localization is a content edit
  in those files.
- **Build-time inlining:** every `NEXT_PUBLIC_*` change needs a rebuild;
  there is no runtime re-theming or per-request brand resolution.
- **Deploy identifiers are file edits:** the Worker name and custom-domain
  route in `wrangler.jsonc` are not env-driven; each merchant deployment
  edits that file.
- **Default fallbacks are the template's own values:** unset brand/contact
  env vars fall back to the template brand strings and the template's default
  support inbox — override all of them per merchant.
- **`NEXT_PUBLIC_ADMIN_ORIGIN` is the exception:** it has **no** default (no
  localhost fallback), and a missing value fails `next build`; seed it in
  `.env.local` from `.env.local.example`.

---

## 8. Rebrand checklist — the 1-hour runbook

The claim this runbook makes real: **identity, palette, fonts, contact
channels, return policy and nav/footer in about one hour.** It is NOT a
catalogue migration and NOT an arbitrary redesign — the boundary is stated at
the end of this section. §2 remains the reference for the config seams; this
section is the procedure over them. Enforcement lives in
`src/lib/rebrand.test.ts`: a brand-literal lint over the component/app tree
plus a seed-wiring smoke, so a skipped step fails a gate instead of shipping
a half-rebrand. Time budget: steps 1–4 ≈ 15–30 min, step 5 ≈ 15 min.

### 1. Set the brand env vars (≈5 min)

Copy `.env.local.example` to `.env.local` (one env file per merchant) and
fill in:

| Variable | What it drives |
| --- | --- |
| `NEXT_PUBLIC_SITE_NAME` | brand name — header, footer, metadata, JSON-LD |
| `NEXT_PUBLIC_SITE_TAGLINE` | tagline — footer, metadata |
| `NEXT_PUBLIC_SITE_DESCRIPTION` | meta/OG description |
| `NEXT_PUBLIC_SITE_URL` | canonical origin (e.g. `https://shop.example.com`) |
| `NEXT_PUBLIC_SUPPORT_EMAIL` | support inbox (also the SMTP sender) |
| `NEXT_PUBLIC_API_BASE_URL` | this merchant's Django API origin |
| `NEXT_PUBLIC_ADMIN_ORIGIN` | this merchant's Django admin origin — **no default; the build fails if unset** |

Contact channels left empty (`NEXT_PUBLIC_SUPPORT_PHONE`,
`NEXT_PUBLIC_WHATSAPP_*`, `NEXT_PUBLIC_INSTAGRAM_URL`) are hidden in the UI —
that is the designed state, not an omission. The full variable table is §2c.
Nav/footer links are a manifest edit: change the `nav` / `footer` groups in
`src/lib/template.config.ts` and Header, MenuOverlay and Footer follow (§7).

### 2. Edit the theme seed (≈5 min)

In `src/lib/theme.ts`, edit `DEFAULT_SEED` (the seam is documented in §2b):

- `brand` → `--color-bronze` (the single chromatic accent), `accent` →
  `--color-bronze-alt`; both must be 6-digit hex (`#rrggbb`).
- `displayFont` / `bodyFont` → **allowlisted keys only** — see step 4.
- Derived colours (focus ring, danger/success, ink family) stay
  stylesheet-owned and are not seedable; they are contrast-audited as a set
  in `globals.css`.

**After every seed edit, run the contrast guard:**

```powershell
npx vitest run src/lib/theme.test.ts
```

The guard recomputes the WCAG ratio from the hex literals for every pair the
seed moves; a failure prints the recomputed ratio and the floor it missed.
Fix the seed hex and re-run until green. `rebrand.test.ts` then re-checks
that every seed slot drives a real `globals.css` token and is well-formed.

One named exception to keep in step: `src/components/checkout/PaymentLauncher.tsx`
hands the hosted Razorpay checkout a literal `theme.color` hex (the widget
renders outside our document and cannot read CSS variables). The rebrand test
pins that literal to `DEFAULT_SEED.brand` and names the file when it drifts.

### 3. Django admin — Site settings (≈5 min, no rebuild)

Sign in to the Django admin at `<NEXT_PUBLIC_ADMIN_ORIGIN>/admin/` and open
the **Site settings** singleton (Ops section; `ops.SiteSettings` on the
backend). The storefront reads `GET /api/settings/` server-side with a 60 s
cache, so edits appear without a frontend rebuild once the cache lapses.
Resolution priority (`src/lib/server-settings.ts`): **Django admin →
build-time env → hidden**. Fields and fall-backs:

| Admin field | Drives | Falls back to |
| --- | --- | --- |
| `support_email` | footer contact inbox | `NEXT_PUBLIC_SUPPORT_EMAIL` |
| `support_phone` | footer phone row | env, else hidden |
| `whatsapp_number` | `wa.me` deep link | env, else hidden |
| `whatsapp_message` | pre-filled WhatsApp text | env, then template default |
| `instagram_url` | Instagram link | env, else hidden |
| `return_window_days` | returns page + home fact strip | blank = store default; `0` = returns closed |

### 4. Fonts (≈2 min — usually no change)

The seed's font keys are the entire font surface: `playfair-display`,
`plus-jakarta-sans`, `georgia`, `system-sans`. Arbitrary webfonts are
impossible by design — `next/font` resolves faces at BUILD time and the CSP
serves only self-hosted faces (§2.2.5, §2b). Pick from the allowlist; a new
typeface is a code change, not a rebrand step.

### 5. Verify — the exact commands (≈15 min)

```powershell
cd frontend
npx vitest run src/lib/theme.test.ts     # contrast guard: green after step 2
npx vitest run src/lib/rebrand.test.ts   # brand-literal lint + seed wiring: green
npm run build                            # exit 0 — brand strings inlined from .env.local
npx vitest run                           # full suite: all files pass, none skipped
```

Expected outputs: each targeted run reports its file(s) passed with zero
failures; `npm run build` exits 0 and lists every route (a missing
`NEXT_PUBLIC_ADMIN_ORIGIN` fails here, loudly); the full run reports every
test file passed. Any red is a missed step, not an environment quirk — the
rebrand test's failure message names the file still carrying the brand
literal. Then spot-check in a browser: header/footer show the new name, the
accent appears on hover/CTA, contact channels render (or stay hidden when
unset), and the legal/contact pages carry the new identity.

### Boundary — what one hour buys, and what it does not

In scope: **brand identity** (name, tagline, description, URL), **palette +
fonts** (the seed slots), **contact channels** (env + Django admin),
**return-window policy** (Django admin), and **nav/footer links** (manifest
edit) — one rebuild at the end.

Out of scope: **catalogue content** (products, copy and imagery are
backend-owned data), **arbitrary redesign** (layout, motion and the token
system are the template's design, not knobs), currency/locale (`money.ts` is
`en-IN`/`INR`; §7), legal prose (page-component content edits; §7), deploy
identifiers (Worker name/route in `wrangler.jsonc`; §7), and any
feature-flag wiring beyond the two flags §4 lists as consumed.
