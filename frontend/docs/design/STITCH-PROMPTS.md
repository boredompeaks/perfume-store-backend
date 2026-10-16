# Google Stitch prompt file — perfume-store storefront

> **STATUS: FILLED by `design-research`, 2026-10-02. REVISED 2026-10-03 (round 4)
> after the user selected 17 Stitch designs by hand.** Those designs — "Artisanal
> Parfumerie" / *Maison Éther* — are now the authority for the visual direction.
> `UI-UX-SPEC.md` §0b is the provenance contract.
>
> **THE REFS ARE A DESIGN SYSTEM, NOT CONTENT.** "Maison Éther", "Éther
> éculaire", every product name, all copy, all imagery, every price amount, and every
> commercial figure are Stitch placeholders. The refs do already use the rupee sign
> and INR, which is correct — what is fabricated is the **amount**, never the currency.
> **The system transfers. The content does not.** The
> API-binding table in `UI-UX-SPEC.md` §12.D makes each placeholder impossible to
> transcribe by accident.
>
> **The 17 designs drifted**, because Stitch does not carry context between
> generations. Canonical is `#FCF9F3` / `#1C1C18` / `#1E1B19` / `#E5E2DC`. **The
> auth family is entirely on-canonical**; the drifters are the PDP, the products
> listing, the home page and password-reset step 2. See `UI-UX-SPEC.md` §2.1.1.
>
> **Five overrides apply to the refs** and are not questions — the focus ring
> (`#9C6A2E`, not the ref's `#B8AF9F`, which fails 3:1), the settle animation
> (must not hide content by default), no permanent `will-change`, the wipe as
> `transform: scaleX()`, and no autoplay. See `UI-UX-SPEC.md` §3 O1–O5.
>
> **Coverage:** 17 designed routes, 5 derived (`/contact`, `/terms`, `/privacy`,
> `not-found`, `error`), 3 inferred (the three recovery routes). Every per-page
> block below is marked **USER-DESIGNED** or **DERIVED**; see §5.12.

---

---

## How to use this file

Stitch is **human-in-the-loop**. There is no MCP and no API integration. The
workflow is:

1. Read §1 (design system) and paste that prompt into Stitch. Iterate until the
   result is right. **This is the load-bearing step** — every later prompt
   inherits whatever you settle here.
2. For each page block in §3, paste that page's prompt. Iterate.
3. Keep the images you like. **Do not commit them to this repository** and do not
   create a `refs/` directory. That absence is deliberate: the spec must be
   executable from words alone.
4. Hand the selected direction to `frontend-architect`, which builds real
   components — never the flattened screenshot.

**One prompt per page. Never one prompt for the whole site.** A whole-site prompt
produces mush: Stitch averages everything into a generic result. This file is
structured to prevent that, and §2 exists to enforce it.

---

## Prompt-writing rules

Every prompt in this file obeys these. The researcher is graded on them.

**Describe composition and motion, not archetype.** "Editorial split hero with
the bottle photograph bleeding past the right gutter, headline set 2 steps below
the optical centre" — not "a modern hero section".

**State the negative direction.** Tells the model what to avoid as explicitly as
what to want. This is the single highest-leverage line in any prompt.

**Prose only.** No JSX, no code fences containing code, no npm, no component
names, no Tailwind classes. Stitch is a design tool; code in the prompt produces
code-flavoured output.

**Repeat the design-system spine per page.** Palette, type pairing, spacing
rhythm and motion character are restated in each page prompt, because Stitch does
not carry context reliably between generations. Each page block cites which part
of §1 it leans on.

**One accent moment per page.** Name the single signature moment and say the rest
of the page is quiet. That constraint is what makes a page read as designed
rather than decorated.

**Accessibility is not optional.** Contrast intent and focus visibility are stated
where relevant. A design that cannot meet WCAG AA is a rejected result, however
beautiful.

### Never emit these

These are the clichés a real design system exists to prevent. If a prompt would
produce one, the prompt is wrong.

- purple-to-blue (or any) generic gradient backgrounds, buttons, or blobs
- decorative glassmorphism / frosted panels with no function
- glowing or neon borders, drop-shadow soup, excessive blur
- emoji used as icons
- centred-everything with no grid discipline
- rounded-full gradient CTA buttons
- auto-playing hero carousels
- stock-photo hero imagery
- low-contrast pastel-on-white text
- fake trust badges, fabricated review counts, placeholder testimonials
- the same three-column icon/heading/paragraph feature grid repeated per section
- animation with no communicative purpose

---

## 1. Design-system prompt

Owns the palette, the type pairing, the scale, the spacing rhythm, the corner
language, surface and elevation treatment, the motion character, the imagery
direction, and the negative list — **in one generation**, because every later
prompt inherits whatever is settled here. If this prompt is wrong, every page is
wrong in the same way.

**Direction to encode** (from `UI-UX-SPEC.md` §2; do not contradict it):

- restrained-luxury base, theatrical motion as accent
- CSS-first: the motion character must be expressible with `transform`/`opacity`
  so it can ship without a JS dependency
- the accent is **one** moment per page, not a system of motion
- motion is opt-in: a page's resting state is its final state

```
A design system for a small perfume house's online store. Warm ivory paper, deep
warm-black ink, one bronze accent. Restraint everywhere; motion used as an accent
rather than as decoration.

SURFACES AND COLOUR
Page background is a warm ivory, close to the colour of uncoated paper — not
white, not grey. Raised panels are true white, used sparingly, so a panel reads as
a sheet laid on paper. Body text is a very dark warm brown-black, not pure black,
so it sits on ivory without vibrating. Secondary text is a mid warm grey that is
still clearly legible on both surfaces. Exactly one accent: a deep, slightly
reddish bronze. It is used for eyebrows, small labels, hover states and at most
one emphasis per page — never for backgrounds, never for buttons, never as a
wash. There is also a single darker red reserved strictly for errors and nothing
else, and a single deep green reserved strictly for confirmed states.

SPACING
A four-pixel base rhythm. Siblings sit four to eight pixels apart. A heading sits
twenty-four pixels above the thing it introduces. Sections are separated by very
large gaps — the gap between sections is four times the largest gap inside a
section, and that ratio is the signature of the whole system. On a phone the
section gap is half of what it is on a desktop, and the inside gaps do not change.
Never an off-rhythm value.

CORNER LANGUAGE
Every container is a perfect square corner. No rounded corners anywhere: not on
cards, not on buttons, not on inputs, not on panels. The single exception is a
small circular badge holding a count in the header, because a number is a lozenge
and nothing else is.

SURFACES AND ELEVATION
Almost nothing is elevated. Separation is done with a single hairline rule in a
warm off-white. Exactly two soft shadows exist in the entire system: one on the
header once the page has scrolled under it, and one on the full-screen menu
overlay so the page beneath reads as dismissed. Nothing else floats, and no card
casts a shadow on hover — a hovered card lifts two pixels and its hairline simply
darkens.

TYPOGRAPHY
Two families only. A characterful display serif with slightly irregular,
hand-set terminals for every heading, product name and the wordmark; tight
leading, generous size, an optical size that suits the size it is set at. A
humanist sans with a large x-height for all body copy, all labels, all buttons and
every form field, at a comfortable reading size that never shrinks on a phone. No
third family. Money and all numbers are set with evenly spaced digits so a total
does not jitter when it changes. Small uppercase labels are widely tracked. Long
prose is held to a narrow measure — roughly thirty-four characters — however wide
the screen is.

IMAGERY DIRECTION
One photograph per product, and it is the product. Shoot objects alone on a
seamless warm-neutral background, lit with a single soft directional light from
one side so the bottle casts a soft shadow and the glass has a highlight to read.
Portrait orientation. Generous empty space around the object — the bottle should
occupy roughly the middle half of the frame. Shot as if on a still-life table in
a workshop, not on a reflective studio sweep. Grade warm and slightly lifted in
the shadows, low contrast, no colour cast. No props beside the bottle except at
most one, out of focus. No hands, no models, no flowers, no fabric, no ribbons, no
smoke, no water droplets, no confetti, no gift boxes. When a photograph is
missing, the frame holds a single large letterform from the product's name in the
bronze accent on a soft bronze-tinted plate, and that is the finished state, not a
placeholder to apologise for.

TEXTURE
At most one texture in the system: a very faint paper grain over large surfaces,
barely perceptible, never on a photograph and never on text. No vignettes, no
gradient meshes, no light leaks, no bokeh overlays.

MOTION CHARACTER
Motion is expressed only through movement and transparency of whole elements.
Nothing ever animates its own size, its own position in the flow, its own spacing
or its own shadow. The character is: things arrive rather than appear — a short,
soft, decelerating settle of about a fifth of a second, with no bounce and no
overshoot. Things leaving go faster than things arriving, about a sixth of a
second. Hover feedback on a card is a barely perceptible two-pixel lift over a
quarter second, and an underline that wipes in from the left. Anything that
crosses a threshold — a menu opening, a panel appearing — fades and settles
together, never one without the other. Elements far down a page may be given a
very slow, barely-there settle as they come into view, but at most two or three per
page, and never anything the visitor is looking at on arrival.

NEGATIVE DIRECTION — do not produce any of the following
No purple-to-blue or any multi-colour gradient, anywhere, as a background, a
button, a blob, a mesh or a text fill. No frosted or translucent glass panels. No
glowing, neon or gradient borders. No coloured outer glow on anything. No drop
shadow stacks and no soft-shadow soup. No emoji used as icons. No circular
gradient action buttons — buttons are square-cornered and solid. No centred
layout without a grid; every page has a left edge or a deliberate centre axis. No
carousel that advances on its own. No stock photography of people for a product
hero. No low-contrast pastel text on a light ground. No badge of trust, no
customer-count claim, no star rating, no testimonial, no award, no "as seen in".
No percentage-off badge or struck-through price. No more than one large display
type element visible at a time. No icon-above-heading-above-paragraph feature grid
repeated three times down a page. No motion that does not tell the visitor
something changed.

OVERALL FEELING
Expensive, quiet, confident, and deliberately under-designed rather than
maximal. The luxury is in the whitespace, the hairlines, the type and the
absence of noise. A visitor should feel the page is edited rather than assembled,
and that exactly one thing on it is allowed to move.
```

---

## 2. Coverage contract

`design-research` MUST emit one block per row. `frontend-architect` implements
per block, so a missing row is a page nobody builds.

Every row below is **FILLED**. "Accent moment" is the single signature motion for
that page (P2: at most one); "—" means the page has **zero** motion, which is a
decision and not an omission. Full justification per row is in
`UI-UX-SPEC.md` §3 and §5.

| # | Route | Page job (one line) | Accent moment | Status |
|---|---|---|---|---|
| 1 | `/` | Brand statement and entry to the catalogue | **The three page sections settling in as they are reached** — one `view()` timeline per section, two of them, everything else still | ✅ §3.1 |
| 2 | `/products` | Product listing — browse and filter | **None.** A comparison page must not move; only the result count acknowledges a filter resolving | ✅ §3.3 |
| 3 | `/products/[slug]` | Product detail — the money page | **The story image drifting as the notes beside it are read** — one sticky frame on a scroll timeline, desktop only. **The bottle itself never moves: it is the largest painted element and moving it would delay the page** | ✅ §3.2 |
| 4 | `/cart` | Cart review and quantity edit | **The line that is removed fading out as the remaining lines close up** — a browser view transition, no hand-written height animation | ✅ §3.3 |
| 5 | `/checkout` | Address, delivery, review, payment | **The one-pixel progress rule filling as the step advances.** The panel content does not animate; the money states do not animate at all | ✅ §3.4 |
| 6 | `/orders` | Order history | **None.** A ledger does not move; only the row hairline darkens on hover | ✅ §3.5 |
| 7 | `/orders/[id]` | Single order + live tracking | **The tracking markers filling as the trail is read** — one per parcel, oldest first. The latest status is also printed as text at the top, so nothing depends on the motion | ✅ §3.5 |
| 8 | `/track-order` | **New page** — public shipment tracking, no account needed | **The same tracking markers**, and nothing at all before a lookup returns | ✅ §3.6 |
| 9 | `/returns` | Return policy and how to request a return | **None.** A policy page that animates is a policy page nobody trusts | ✅ §3.7 |
| 10 | `/shipping` | Shipping policy | **None.** Same reasoning | ✅ §3.7 |
| 11 | `/login` | Sign in | **None.** A form that fades in suggests the fields were not always there | ✅ §3.9 |
| 12 | `/register` | Account creation | **None.** Same reasoning | ✅ §3.9 |
| 13 | `/forgot-password` · `/forgot-username` · `/reset-password` | Credential recovery | **None, by rule.** These pages are used by someone who is locked out; they get the quietest treatment in the system | ✅ §3.10 |
| 14 | `/verify-email` · `/resend-verification` | Verification | **None, by rule.** Same reasoning | ✅ §3.10 |
| 15 | `/contact` | Contact and enquiry | **None.** Four real channels and no form; there is nothing to sequence | ✅ §3.8 |
| 16 | `/terms` · `/privacy` | Legal | **None.** | ✅ §3.11 |
| 17 | `not-found` · `error` | Branded dead ends | **None.** A 404 that animates is a 404 that feels like a loading state | ✅ §5, out-of-scope block |

**Two corrections to the skeleton's route descriptions**, made against source
rather than left standing:

- **Row 9, `/returns`:** the contract says "Return request and status". There is
  **no customer-facing returns endpoint** — the only returns machinery is a
  staff-gated admin refund. The page is a **policy page** whose "how to request"
  section is a pre-filled email link, and this redesign keeps it that way.
  Building a form that posts nowhere would collect a customer's address and
  reason and discard both. Recorded as `UI-UX-SPEC.md` §12-G13.
- **Row 8 is new.** The contract did not list `/track-order`, and spec §3.9 does.
  The backend has shipped the endpoint it needs, so the page is now buildable and
  is the largest single addition this redesign makes.

**Explicitly out of scope for this redesign** — the contract asks for the
decision and the reason, so here it is:

- **`/admin`** — the admin is Django's, served on the **API origin**, and
  `src/app/admin/page.tsx` is a five-line redirect to `ADMIN_URL`. There is no
  storefront `/admin` to design. Spec §5 and §13 both require the admin to be a
  *separate application surface* with its own layout, navigation tree and access
  control, and the backend has already built it behind an RBAC capability layer.
  Designing a second admin theme here would duplicate a surface that exists and
  does not belong to a luxury-storefront brief. **KEEP the redirect as it is.**
- **`/staff/login` · `/staff/mfa/enroll`** — role-gated operational surfaces, and
  the same argument applies with more force. The MFA enrollment screen's job is to
  get a privileged account onto a device; its design brief is "unmistakable", not
  "expensive". The current build already gets this right: same tokens, same focus
  ring, same field styling, `noindex`. **KEEP both exactly as they are.**
  One structural fix is *not* a redesign and is recorded so it is not mistaken for
  one: both currently render inside the storefront `<main>` with the customer
  header and footer around them. They should get a route group with their own
  minimal layout — wordmark and form, nothing else. That is a layout change with a
  functional motive, not a visual one.
- **No mega-menu, no cart drawer, no wishlist, no reviews section, no newsletter
  form, no homepage editor.** The first two are ruled out by `PLAN.md` §4 and §5
  already; the rest have no endpoint behind them (`UI-UX-SPEC.md` §12-G9, G16).
  Their absence is recorded with a reason rather than left silent.

---

## 3. Per-page blocks

Each block uses this shape. **The `products/[slug]` block below is the worked
example** — it shows the expected density and specificity.

### 3.x — `<route>` — block shape

Each block below uses this shape. **The `products/[slug]` block is the worked
example** — it shows the expected density and specificity, and it is deliberately
not deleted, because it is the density bar every other block is measured against.

**One honest divergence from the worked example**, so the architect is not caught
out: it specifies "one slow scale-and-settle on the gallery image as it enters".
**This spec overrides that.** The gallery image is the largest painted element on
the product page, and animating it defers that measurement. The entry motion moves
to the purchase column beside it instead, and the scroll-linked drift applies only
to the image *below* the fold. See `UI-UX-SPEC.md` §3 for the reasoning.

- **Job:** one line.
- **Sections, in order:** numbered, each with its composition intent.
- **Accent moment:** the single signature motion, and why it earns its place.
- **Responsive notes:** the concrete differences at phone / tablet / desktop.
- **Negative direction:** page-specific avoid-list.
- **Spine:** which parts of §1 this leans on.

```
PASTE STITCH PROMPT HERE
```

---

### 3.2 — `/products/[slug]` — WORKED EXAMPLE (do not delete)

- **Job:** sell one fragrance and answer every pre-purchase doubt without
  leaving the page.
- **Sections, in order:**
  1. Gallery — the bottle as the only subject, generous negative space, image
     carries the page.
  2. Purchase block — name, price, size selector, add-to-cart. Sticky on
     desktop, docked to a bottom bar on phone.
  3. Fragrance notes — top / heart / base as three columns that collapse to a
     stack. Typography carries this; no icons.
  4. Story — short editorial paragraph, one supporting image.
  5. Pairing / wear guidance — practical copy, quiet treatment.
  6. Delivery and returns — the configured return window from
     `GET /api/settings/`, stated plainly. A deadline a customer cannot see is
     one they cannot meet.
- **Accent moment:** the gallery. One image, one slow scale-and-settle on entry
  plus a scroll-progress-linked parallax at low amplitude. It earns its place
  because product photography *is* the content. Everything below the fold stays
  still.
- **Responsive notes:** phone — single image, swipeable gallery, sticky purchase
  bar; the notes and story sections move above the fold-adjacent break. Desktop —
  two-column gallery + purchase, notes in a three-up row.
- **Negative direction:** no carousel with dots and arrows, no "customers also
  bought" carousel, no urgency countdown, no star-rating row unless real review
  data exists, no floating discount badge.
- **Spine:** palette and type from §1; motion from §1's motion character; imagery
  direction from §1.

```
A luxury fragrance product detail page for a perfume house. Full-bleed gallery
of a single perfume bottle as the only subject, shot on a seamless warm-neutral
background with soft directional light, generous negative space around the
object. The image is the page: no collage, no thumbnail grid, no carousel
chrome.

Below the gallery, a two-column purchase block — product name set large in a
refined serif, price in a restrained sans beneath it, a compact size selector
as small labelled rectangles rather than a dropdown, and a single solid
add-to-cart button in deep ink on a bone background. No rounded pill, no
gradient, no glow.

Then three columns for the fragrance notes — top, heart and base — set as
typographic groupings with no icons between them, collapsing to a single stack
on narrow screens.

A short editorial paragraph about the scent's character with one supporting
still-life image, generously spaced. Then a practical wear-guidance section in
smaller type. Then a delivery and returns line stating a 30-day return window in
plain language.

Accent motion: one slow scale-and-settle on the gallery image as it enters, and
a very low-amplitude scroll-linked drift on that image only. Everything below
the gallery is completely still.

Avoid: carousels with arrows or dots, "customers also bought" carousels, countdown
urgency timers, star ratings unless real review counts exist, floating discount
badges, purple or blue gradients, glassmorphism, neon glow, emoji as icons,
centred-everything layouts.

Overall feeling: expensive, quiet, confident. Restraint everywhere except the
gallery, which is the one moment allowed to move.
```

### 3.1 — `/` — home — **USER-DESIGNED, canonical**

> **This block is superseded by the design the user selected.** It is retained
> because `UI-UX-SPEC.md` §3.1 and §5.12.1 reference it, and because the prompt
> remains the specification for the five pages with **no** design (§5.12.2).
>
> **The selected design** (`maison_ther_catalogue_accueil_carousel_edition`)
> implements **exactly the option-(a) accent this block and `UI-UX-SPEC.md` §3.1
> already specify** — a scroll-triggered settle reveal — **plus a manual carousel
> track**, which is new to this spec and is now specced in §2.4 (`track`) and §3
> (override O5). **The 3D cube option was considered and declined** by the
> stakeholder; its measured reason is in `MOTION-3D-ANALYSIS.md` §5.5 and
> `UI-UX-SPEC.md` §11.5.3.

- **Job:** say what the house is, and get the visitor into the catalogue.
- **Sections, in order:**
  1. **Hero.** A left-aligned column on warm ivory (`#FCF9F3`), not a full-bleed
     photograph. A small widely-tracked uppercase **bronze** eyebrow (`#8C5338`,
     11px, `0.2em`) reading that these are extraits, made in small batches. Then a
     very large Playfair headline, tight and confident, with **one** word in bronze
     italic and nothing else emphasised. A short paragraph in Plus Jakarta Sans at
     `--measure`, then two actions: one solid deep-ink (`#1E1B19`) square-cornered
     button at 48px minimum height with `0.1em`-tracked uppercase, and a plain text
     link whose underline **wipes in from the left** on hover.
  2. **The carousel rail.** Full-width panels on a single track, moved by
     `transform` only, at 500ms on a dedicated curve. **Manual controls only:**
     previous and next buttons that go `disabled:` at the ends, four dot buttons,
     and an `01`/`04` counter. **No autoplay** — see §3 O5. This is the *narrative*
     rail and it does not count as the page's accent moment, because it only
     moves on a deliberate click.
  3. **New arrivals.** A hairline rule, a section heading in Playfair with a
     "View all" link on the far right, and a 4-up grid at desktop, 3-up at tablet,
     2-up on mobile. Each product: one portrait photograph of the bottle alone on a
     seamless warm background, soft side light, generous empty space, on a white
     plate framed by a 1px `#E5E2DC` hairline. Beneath: the name in Playfair, the
     price right-aligned in tabular numerals, the volume in small muted grey.
  4. **House position.** A hairline rule, then two columns: a bronze eyebrow and a
     Playfair heading on the left, two quiet paragraphs on the right at
     `--measure`.
  5. **Fact strip.** Three verifiable facts in small tracked capitals — ships
     across India, secure payment, a return window — separated by faint centred
     dots. **The window is bound to `/api/settings/` and renders nothing until that
     field exists** (`UI-UX-SPEC.md` §12.D.2). Plain type; no badges, no logos.
- **Accent moment:** **the settle reveal.** Sections fade and settle 8px into place
  as they are reached — specified as `animation-timeline: view()`, **not** the
  design's `IntersectionObserver` script, because the default state must be the
  final state (`UI-UX-SPEC.md` §3, override O4). One moment. The carousel is
  user-initiated and does not count against it.
- **Responsive notes:** 2-up grid and 64px section gap at mobile; 3-up and 96px at
  tablet; 4-up and 128px at desktop with 64px margins and 32px gutters.
- **Negative direction:** no hero photograph, no autoplay, no swipe-only
  navigation, no full-bleed image, category tiles, testimonials, review stars,
  customer counts, newsletter form, percentage-off badges, trust logos, scrolling
  announcement bar, gradients, `backdrop-filter`, glow, emoji as icons, rounded
  buttons, or centred-everything layouts.
- **Spine:** §1 palette and type; §2 tokens; motion from §2.4's intent classes.

```
A perfume house's home page. Warm ivory paper, deep warm-black ink, one bronze
accent, square corners, hairline rules, and exactly one thing that moves on its
own.

An open, left-aligned hero on the ivory — no photograph. A small widely-tracked
uppercase bronze eyebrow reading that these are extraits de parfum, made in small
batches. Beneath it a very large display serif headline, tight and confident, with
one single word set in bronze italic and nothing else emphasised. A short
paragraph below in a warm grey sans, held to a narrow measure. Then two actions
side by side: a solid deep-ink square-cornered button, and a plain small text link
whose underline wipes in from the left on hover. The column sits slightly above the
optical centre, leaving air beneath it.

Then a full-width editorial carousel: large panels of a single perfume bottle alone
on a seamless warm background, lit softly from one side with generous empty space
around the object. Beneath the panels, previous and next square hairline-bordered
buttons, four small dot buttons, and a two-digit counter showing which panel is
showing and how many there are. The previous button is disabled on the first panel
and the next is disabled on the last. Movement between panels is manual only — it
never advances by itself, and it is controlled by those buttons and dots.

Then a hairline rule, then a section: a display serif heading reading New arrivals
with a small "View all" text link pushed to the far right on the same baseline.
Below it a four-column grid of products. Each product is a portrait photograph of a
single bottle alone on a seamless warm background with soft side light and lots of
empty space, on a white plate framed by a single hairline. Beneath the plate the
fragrance name in the display serif, the price right-aligned in the sans with
evenly spaced digits, and the volume in a smaller warm grey underneath. Nothing
else on the card: no rating, no badge, no strikethrough price, no ribbon.

Another hairline rule, then a two-column editorial block. Left: a short bronze
eyebrow and a display serif heading about building around a single idea until it
holds. Right: two quiet paragraphs in warm grey explaining that the house releases
few perfumes rather than a large catalogue, and that everything stated on the page
is what is in the bottle. Narrow measure, generous leading.

Then a single line of three short facts in small tracked capitals, separated by
faint centred dots: that orders ship across India, that payment is handled by a
named secure provider, and the return window. Plain type, no icons, no logos, no
badges.

Accent motion, and only this: each section settles very slightly into place as it
is first reached, a soft fade with a short eight-pixel downward travel over about a
fifth of a second. The hero does not move. The carousel moves only when a visitor
presses a button or a dot.

Avoid: autoplay or any self-advancing slider, swipe-only navigation without
buttons, hero photography, full-bleed imagery, category shortcut tiles,
testimonials, review stars, customer counts, newsletter signup, percentage-off
badges, trust logos, a scrolling announcement bar, gradients, frosted or glass
panels, glow, emoji as icons, rounded buttons, and centred-everything layouts.

Overall feeling: the first page of a printed catalogue that happens to be
interactive. Expensive because it is quiet.
```

---

### 3.3 — `/products` — product listing

- **Job:** let someone compare and narrow a catalogue without being entertained.
- **Sections, in order:**
  1. Page title in the display serif, left-aligned, nothing above it but the
     header.
  2. **Filter row.** One horizontal row on a hairline rule: a search field, then
     a minimum price and a maximum price field with a bordered "Apply" button,
     then a sort control pushed to the right. On a phone it wraps to two rows.
     If a category filter is present in the address, it appears below as a
     small removable chip. **There is no category picker** — the store has no
     category list to draw.
  3. A single line of muted text stating how many products matched.
  4. **Grid.** Two columns on a phone, three on a tablet, four on a desktop.
  5. Pagination: previous, a window of numbered pages with gaps, next.
- **Accent moment:** **none.** A comparison page must not move — anything that
  shifts while someone is comparing prices is a defect. The only motion is the
  count line quietly refreshing when a filter resolves, and the card hover.
- **Responsive notes:** phone — two columns, filters wrapped, pagination as a
  compact row of numbers. Tablet — three columns. Desktop — four columns within a
  fixed maximum width, so the gutters grow on a wide monitor rather than the
  cards.
- **Negative direction:** no per-card fade-in on scroll and no staggered reveal
  (it re-fires on every filter change), no "customers also bought", no recently
  viewed, no faceted sidebar, no colour swatches, no size filter, no category
  dropdown that would have nothing in it.
- **Spine:** §1 entirely; the grid is the §1 image direction applied twelve times.

```
A product listing page for a perfume house, designed to be compared in rather
than looked at.

Warm ivory page. A left-aligned page title in a large display serif reading that
all fragrances are shown. No eyebrow above it, no subtitle.

Below, a single horizontal control row sitting on a hairline rule, with a small
amount of air above it. On the left a plain rectangular search field with a visible
warm-grey border and a soft ivory fill, placeholder text in a legible grey. In the
middle, two small rectangular price fields side by side with placeholder text
showing a rupee symbol, followed by a bordered square-cornered Apply button. On
the far right, a small muted label and a rectangular sort dropdown. Everything is
square-cornered, hairline-bordered, and the same height.

Beneath the control row, one short line of small warm-grey text: how many
products there are.

Then the grid: four columns on a wide screen, three on a tablet, two on a phone,
with generous even gutters. Each product is a portrait photograph of a single
bottle alone on a seamless warm background with soft side light and lots of empty
space, on a soft bronze-tinted plate. Beneath the plate, the fragrance name in
the display serif with the price right-aligned on the same baseline in the sans
with evenly spaced digits, and the volume in small warm grey underneath. The whole
card is clickable. On hover the photograph scales up by about three percent over a
quarter second and a faint hairline border appears around the card. Sold-out
products carry one small solid dark chip reading Sold out at the top-left of the
plate, and nothing else.

Beneath the grid, a centred row of pagination: a previous link, a window of
numbered pages with gaps marked by faint dots, and a next link. The current page
is a solid dark square with ivory numerals. Disabled edges are simply faint.

Accent motion: none on this page. No element enters on scroll, no card staggers,
no counter rolls. The count line cross-fades quietly when a filter resolves and
nothing else responds. Hover on a card is the only movement.

Avoid: per-card scroll reveals or staggered entrances, infinite scroll, a
faceted sidebar, colour or size swatches, a category dropdown with nothing in it,
star ratings, discount badges, struck-through prices, "customers also bought",
"frequently bought together", a recently-viewed strip, trust badges, gradients,
glass panels, rounded cards, and centred page titles.
```

---

### 3.4 — `/cart` — cart review

- **Job:** let someone check what they are buying and change their mind about a
  quantity without ceremony.
- **Sections, in order:**
  1. Page title in the display serif.
  2. **Line items.** Each row: a small portrait thumbnail on the left linking to
     the product, then the fragrance name in the display serif with volume and
     unit price beneath in muted grey, and the line total right-aligned in evenly
     spaced digits. Beneath, a row holding a quantity stepper on the left — two
     square bordered buttons around a centred number — and a plain underlined
     "Remove" link on the right. Rows separated by hairlines, generous vertical
     space.
  3. **Summary panel**, right-hand column on a desktop, below the items on a
     phone. Bordered with a hairline, no fill difference, no shadow. Heading in
     the display serif, then a subtotal row and a small muted line stating that
     the final total is confirmed at checkout, then a hairline, then the coupon
     field — a rectangular input and a bordered Apply button — then a hairline,
     then one full-width solid dark button reading "Checkout as guest", with a
     plain underlined "or sign in" link beneath it.
- **Accent moment:** **the line that is removed.** It fades out as the remaining
  lines close the gap upward, so the customer sees the change rather than having
  a row blink out of existence. It earns its place because it answers "did that
  work?" without a word.
- **Responsive notes:** phone — the summary panel moves below the items, full
  width; each line stacks its thumbnail, name and total with the stepper and
  Remove on a second row. Desktop — items in a wide left column, a fixed
  three-hundred-and-sixty-pixel summary column on the right that stays in view as
  the list scrolls.
- **Negative direction:** no slide-in cart drawer, no free-shipping progress bar
  (no threshold data), no "you may also like", no gift-wrap upsell, no quantity
  dropdown, no animated numbers, no green tick on the checkout button.
- **Spine:** §1; the panel is a hairline box on the same ivory, never a raised
  card.

```
A cart review page for a perfume house. Warm ivory, deep ink, one bronze accent,
square corners, hairline rules.

A left-aligned page title in a large display serif. Below it, a list of line items
separated by hairline rules with generous space between them. Each line: on the
left a small portrait photograph of the bottle on a soft bronze-tinted plate,
linking to the product; beside it the fragrance name in the display serif, with
the volume and unit price beneath in small warm grey; and at the far right the
line total in the sans with evenly spaced digits. Below that, a row with a
quantity control on the left — two square hairline-bordered buttons showing a
minus and a plus around a centred number — and on the right a small plain
underlined link reading Remove. Disabled minus and plus states are faint.

To the right of the list on a wide screen, a narrow summary panel outlined with a
single hairline, sitting on the same ivory with no shadow and no fill difference.
A display serif heading reading Summary. A row with Subtotal on the left and the
amount on the right in evenly spaced digits. Beneath it, in small warm grey, a
sentence saying the final total is confirmed at checkout. A hairline. A coupon
field: one rectangular input with placeholder text reading Coupon code, and a
bordered square Apply button beside it. Another hairline. Then one full-width
solid deep-ink button reading Checkout as guest. Beneath it, centred, a small
plain underlined link reading or sign in, and a small muted line saying the cart
is saved on this device.

On a phone the summary panel drops below the list and goes full width, and each
line stacks so the quantity control and the remove link sit on their own row.

Accent motion, and only this: when a line is removed, that line fades out while
the lines below it close the gap upward, over about a fifth of a second. Nothing
else on the page animates. No count-up on the subtotal, no progress bar, no
shimmering.

Avoid: a slide-in cart drawer, a free-shipping threshold bar, a recommended
products strip, a gift-wrap or gift-message upsell, a quantity dropdown, animated
number counters, a green confirmation tick, coloured badges, gradients, glass
panels, rounded panels, drop shadows on the panel, and centred page titles.
```

---

### 3.5 — `/checkout` — address, delivery, review, payment

- **Job:** collect what is needed, show what it costs, and get the visitor to
  the gateway without ever making them guess.
- **Sections, in order:**
  1. Page title in the display serif.
  2. **Step indicator.** Three labels in small tracked capitals — Delivery,
     Review, Pay — with the completed portion of a one-pixel rule between them
     filled solid and the rest faint, and the current label in full ink while the
     others are muted.
  3. **Step one, Delivery.** A two-column field grid: full name, email, phone,
     then address spanning both columns, then city, state and pincode. Every
     field is a rectangular hairline-bordered input with a small label above it.
     Beneath the pincode, a plain line where a delivery price will appear.
     Beneath the fields, a bordered note explaining that details are saved on this
     device only, with a plain underlined control to forget them. Then a solid
     dark button continuing to the review step.
  4. **Step two, Review.** A hairline-divided list of what is being bought with
     amounts right-aligned, a line for the delivery charge, a coupon field, then
     two buttons side by side: an outlined Back and a solid dark Create order.
  5. **Step three, Payment.** The order reference and the server-computed total in
     evenly spaced digits, then one solid dark button opening a secure payment
     window.
  6. **Order summary**, a narrow hairline-outlined column on the right on desktop,
     below the step on a phone: the subtotal, the order total once it exists, and
     a small muted line naming the payment provider and stating that card details
     never reach the store.
- **Accent moment:** **the progress rule filling as the step advances.** It earns
  its place because it answers the only question this page raises — how far along
  am I — and it does so without moving anything the visitor might be about to
  click. **The step content itself does not animate, and the payment states do not
  animate at all.**
- **Responsive notes:** phone — one column, summary below the step, step labels in
  a single row. Desktop — the step in a wide left column and the summary in a
  fixed narrow right column that stays in view.
- **Negative direction:** no animated slide between steps, no accordion, no
  progress spinner over the submit button that moves the button, no card-number
  formatting gimmicks, no coloured step badges, no order-summary image
  thumbnails competing with the step, no "trust badges" beside the payment
  button.
- **Spine:** §1; fields are §1's field styling, which is the same on every page.

```
A checkout page for a perfume house, in three steps on one page. Warm ivory,
deep ink, one bronze accent, square corners, hairline rules, no shadows.

A left-aligned page title in a large display serif. Directly beneath it, a step
indicator: three small tracked capital labels reading Delivery, Review and Pay,
with a single hairline rule running between them. The portion of the rule up to
the current step is solid deep ink; the remainder is faint. The current step's
label is in full ink, the others in warm grey.

Step one is a field grid, two columns on a wide screen and one on a phone. Full
name, email and phone across the first row; address spanning both columns; then
city, state and pincode. Every field is a plain rectangle with a single visible
hairline border, an ivory fill, a small label above it in the sans, and a
comfortable height. Placeholder text, where used, is a clearly legible warm grey
and never lighter. Beneath the pincode field, a plain single line of warm-grey
text with room reserved for a delivery price to appear.

Below the fields, a plain rectangular note outlined with a hairline, containing
small text explaining that these details are saved on this device only and are
never shared, and a plain underlined control to forget them.

Then a solid deep-ink button reading Continue to review, left-aligned.

Step two replaces the fields with a hairline-divided list of what is being bought:
each row a fragrance name on the left and an amount with evenly spaced digits on
the right. Beneath the list, a line for the delivery charge and a line for the
total, then a coupon input with a bordered Apply button. Then two buttons side by
side: an outlined Back button and a solid deep-ink Create order button.

Step three shows the order reference in the display serif and the server's total
in the sans with evenly spaced digits, then one solid deep-ink button reading Pay
with the amount. A small muted line explains that a secure payment window will
open.

On the right of the step, a narrow summary panel outlined with a single hairline
on the same ivory, with a display serif heading, the cart subtotal, the confirmed
order total once it exists, and a small muted line naming the payment provider and
stating that card details never reach the store. On a phone this panel drops below
the step.

Accent motion, and only this: the filled portion of the step rule grows to the new
position over about a fifth of a second. The step content itself does not move,
fade or slide. The payment states do not animate at all — an error, a cancelled
payment, or a completed payment appears immediately, with no transition on the
element that carries the retry or continue control.

Avoid: a sliding or fading transition between steps, an accordion that animates
its height, a spinner that shifts the submit button, animated number counters,
coloured step badges, progress bars with percentages, trust badges beside the
pay button, card-brand logo walls, gradients, glass, glow, rounded fields, and
drop shadows on the summary panel.
```

### 3.6 — `/orders` — order history

- **Job:** let someone find one past order and know its state at a glance.
- **Sections, in order:** page title in the display serif, then a hairline-divided
  list. Each row is one clickable block: the order reference in the display serif,
  a small uppercase state chip beside it, the date, and the total pushed to the
  far right in evenly spaced digits; beneath, a single muted line summarising the
  contents, truncated with an ellipsis if long.
- **Accent moment:** **none.** A list of transactions is a ledger, and a ledger
  that moves is harder to read. Only the row's hairline darkens on hover.
- **Responsive notes:** phone — each row stacks: reference and chip on line one,
  date and total on line two, contents on line three, truncated. Desktop — one
  row, with the total pushed right.
- **Negative direction:** no green success badges, no progress bars, no card
  thumbnails per row (the detail page has them), no reorder button, no invoice
  icons, no per-row timeline, no "track all", no status filter (no endpoint).
- **Spine:** §1; the state chip is solid ink when live, hairline-outlined and
  muted when not.

```
An order history page for a perfume house. Warm ivory, deep ink, one bronze
accent, square corners, hairline rules.

A left-aligned page title in a large display serif reading that these are your
orders. No subtitle, no eyebrow.

Below it, a list divided by hairline rules, top and bottom, with generous space
between rows. Each row is a single clickable block. On the left, the order
reference in the display serif, in the form of the letters ORD, a year, and a run
of digits. Immediately after it a small uppercase chip: solid deep ink with ivory
lettering when the order is live, or a hairline outline with warm-grey lettering
when it is pending payment or cancelled. Then a small muted date. Pushed to the far
right on a wide screen, the order total in the sans with evenly spaced digits.
Beneath all of that, a single small warm-grey line naming the contents and
quantities, ending in an ellipsis if it is long.

On hover the row's hairline darkens to solid ink and nothing moves. There is no
lift, no scale, no colour wash.

On a phone each row stacks: the reference and chip on the first line, the date and
the total on the second, the contents on the third.

Accent motion: none. Nothing on this page animates.

Avoid: green success badges, red failure badges, coloured status pills with
rounded ends, progress bars or step trackers on each row, product thumbnails in
the list, reorder buttons, invoice or download icons, a per-row timeline, a
"track all" link, a status filter with nothing behind it, hover lifts, gradients,
glass, rounded rows, and drop shadows.
```

---

### 3.7 — `/orders/[id]` — single order and live tracking

- **Job:** answer "where is my order" completely, on the page, without a phone
  call.
- **Sections, in order:**
  1. A small underlined "all orders" link.
  2. A confirmation banner, when the customer has just paid: a hairline-outlined
     panel reading that payment was received and thanking them, with the order
     reference beneath.
  3. Header row: the order reference in the display serif at large size, the
     state chip, the date.
  4. **Lifecycle timeline.** A vertical hairline rule with six steps down it —
     placed, paid, dispatched, delivered, and where applicable cancelled or
     refunded. Steps that have happened are solid ink with their date beside them;
     steps that have not are a faint outline with just their label and no date.
     **The unreached steps stay visible on purpose** — knowing what is still to
     come is the actual question.
  5. **Shipment tracking**, one block per parcel, oldest first, because an order
     may be split. Each block: the parcel state in the display serif, the carrier
     name, the tracking number in a small outlined row with evenly spaced digits
     that is easy to select and copy, an estimated delivery date, and beneath it a
     short indented list of events, each with a timestamp and a short status word.
  6. **Items and totals** in a wide left column: a hairline-divided list of what
     was bought with amounts right-aligned, then subtotal, any discount, the
     delivery charge, and the total in a heavier weight with a hairline above it.
  7. **Delivery address** in a narrow hairline-outlined right column, set as an
     address block with a name, street, city, state, postal code and phone number.
     Where payment is still outstanding, a small panel below it explaining that
     the amount is held server-side, with one solid button to complete it.
- **Accent moment:** **the tracking markers filling as the trail is read.** One
  marker per event, revealing as its row is reached, so reading position maps onto
  journey position. It earns its place because a parcel's history is a narrative,
  and it is safe because the same information is printed as text at the top — a
  customer with reduced motion, a crawler or a screen reader loses nothing.
- **Responsive notes:** phone — single column, address below totals, the timeline
  rule in a narrow left gutter with the event text beside it. Desktop — items and
  totals wide on the left, address and tracking panels in a narrow fixed right
  column.
- **Negative direction:** no animated map, no carrier logo wall, no "share
  tracking" button, no countdown to delivery (no reliable date exists), no green
  tick parade, no confetti, no progress percentage, no reorder at this stage, no
  "buy again" carousel, no map embed.
- **Spine:** §1; the timeline is the §1 hairline language with one filled state.

```
An order detail page for a perfume house, with live shipment tracking. Warm ivory,
deep ink, one bronze accent, square corners, hairline rules, no shadows.

At the top, a small plain underlined link reading all orders. Beneath it,
conditionally, a confirmation panel outlined with a single hairline and no fill
difference, with a display serif heading reading that payment was received and
thanking the customer, and the order reference beneath it in small warm grey.

Below, a header row: the order reference in a large display serif, a small
uppercase state chip beside it — solid deep ink when live, hairline-outlined and
muted when payment is pending — and a small muted date.

Then a vertical timeline. A single hairline rule runs down the left with six step
markers on it, each a small circle. The first four steps read placed, paid,
dispatched and delivered; two more, cancelled and refunded, are shown faded out
because they only apply sometimes. Steps that have happened are filled solid deep
ink with their date and time in small warm grey beside them. Steps that have not
happened yet are shown as faint hollow circles with their label and no date at all
— they stay visible on purpose, so the customer can see what is still to come.

Then shipment tracking, one block per parcel, the oldest parcel first, separated
by hairline rules. Each block opens with the parcel's state in the display serif.
Beneath it a small two-column list: the carrier, then a tracking number set with
evenly spaced digits inside a plain outlined rectangle so it can be selected and
copied easily, then an estimated delivery date. Beneath that an indented list of
events, each one a timestamp in small warm grey followed by a short status word in
full ink.

Then a two-column body. Left, wide: a small tracked uppercase label reading Items,
then a hairline-divided list where each row has a fragrance name and quantity on
the left and an amount with evenly spaced digits on the right. Beneath it a
summary: a subtotal row, a discount row if there is one, a delivery charge row,
and then the total in a heavier weight with a hairline above it.

Right, narrow and hairline-outlined: a small tracked uppercase label reading
Delivery to, then the address set as an address block with a name, street line,
city, state and postal code on separate lines, and a phone number. Where payment
has not been completed, a hairline-separated panel below it with small text
explaining the amount is held securely on the server, and one solid deep-ink
button to complete the payment.

On a phone everything becomes a single column with the address below the totals,
and the timeline rule sits in a narrow left gutter.

Accent motion, and only this: the timeline markers fill in as each one is reached
while reading down the page, over about a fifth of a second, so reading position
maps onto journey position. Nothing else moves. No panel fades, no section
reveals on scroll, the timeline does not animate on load.

Avoid: an animated map or map embed, carrier logo walls, a share-tracking button,
a countdown timer to delivery, a delivery progress percentage, green tick marks
in sequence, confetti, a reorder button at this stage, a "buy again" carousel,
coloured status pills, gradients, glass panels, drop shadows on the panels, and
rounded corners.
```

---

### 3.8 — `/track-order` — public tracking, no account

- **Job:** let someone with only an order reference find their parcel.
- **Sections, in order:**
  1. Page title in the display serif reading that an order can be tracked.
  2. A short paragraph in muted grey explaining that an order reference is needed,
     and that anyone who ordered as a guest also has an access code from their
     confirmation.
  3. One small form, two fields stacked with labels above: **order reference**,
     required; **access code**, optional. Then one solid dark button.
  4. On a lookup that finds nothing, one hairline-outlined panel containing a
     single short sentence. **It must be identical whatever went wrong** — no
     distinction between an unknown reference, a wrong code, someone else's order,
     and an order that has not shipped yet. Revealing which one it was would turn
     the page into a way to test whether someone's order exists.
  5. On a lookup that succeeds, the same tracking blocks described in the order
     detail page above.
- **Accent moment:** the tracking markers, and **nothing at all before a lookup
  returns.** The empty state is completely still — an empty page that animates
  looks like a page that is loading.
- **Responsive notes:** phone — single column, both fields full width, the button
  full width. Desktop — the form column held to a narrow measure and left-aligned
  rather than centred.
- **Negative direction:** no account-creation prompt, no marketing, no "while you
  are here" product grid, no illustration, no map, no carrier logos, no countdown,
  and **never a different message for a miss than for a wrong code.**
- **Spine:** §1; a narrow left-aligned column on ivory, no card, no panel fill.

```
A public order-tracking page for a perfume house, usable without an account. Warm
ivory, deep ink, one bronze accent, square corners, hairline rules.

A narrow left-aligned column on the ivory page — not centred, and not inside a
card. A page title in a large display serif about tracking an order. Beneath it, a
short paragraph in warm grey explaining that an order reference is required, and
that anyone who placed an order without an account also has an access code from
their confirmation message.

Then one small form with two stacked fields, each with a small label above a plain
hairline-bordered rectangle: an order reference, and an access code marked
optional. Placeholder text in a clearly legible warm grey. Then one solid deep-ink
button reading Track my order.

When a lookup returns nothing, one plain rectangular panel outlined with a single
hairline containing a single short sentence saying the order could not be found.
The panel must read identically whether the reference is unknown, the code is
wrong, the order belongs to someone else, or the order simply has not shipped yet.
There is no second message, no help-text variant, and no way for a visitor to tell
the cases apart.

When a lookup succeeds, the parcels are listed oldest first, separated by hairline
rules, exactly as in the order detail page: the parcel state in the display serif,
then the carrier, a tracking number with evenly spaced digits inside a plain
outlined rectangle, an estimated delivery date, and an indented list of events each
with a timestamp and a short status word.

Accent motion: none at all before a lookup is submitted. The empty page is
completely still. After a successful lookup, the event markers fill in as each is
reached, over about a fifth of a second.

Avoid: a sign-up prompt, a newsletter form, a "while you are here" product grid,
an illustration or empty-state drawing, a map, carrier logos, a countdown timer,
a different message for a failed lookup than for a wrong code, coloured banners,
gradients, glass, rounded panels, and drop shadows.
```

---

### 3.9 — `/returns` and `/shipping` — policy

- **Job:** state the policy plainly enough that a customer can meet it.
- **Sections, in order:** the house name as a small bronze eyebrow, the page title
  in the display serif, a muted "last updated" line, then four or five sections
  each with a display serif subheading and prose in muted grey at a narrow measure,
  separated by generous space. Then a hairline and a closing block giving the
  support email as a plain underlined link and cross-links to the other two policy
  pages.
- **Above the prose**, one short scannable pair of facts: the return window and
  the dispatch window, as two label-and-value pairs. This is the only thing most
  visitors open the page to find, so it must be findable without reading.
- **Accent moment:** **none. Zero.** A legal or policy page that animates is a
  policy page nobody trusts.
- **Responsive notes:** phone — one column, the facts pair above the first
  section, no table of contents. Desktop — a narrow table of contents in a left
  column and the prose beside it, both at a readable measure. The contents is a
  plain list of in-page links with **no highlighting of the current section** —
  that would need a scroll listener for no benefit.
- **Negative direction:** no accordion that animates its height, no "expand all",
  no reading-progress indicator, no highlighted current section, no print icon, no
  share button, no "was this helpful".
- **Spine:** §1 entirely. Prose at a narrow measure, muted grey, the single accent
  used in the eyebrow only.

```
A returns policy page and a shipping policy page for a perfume house, sharing one
layout. Warm ivory, deep ink, one bronze accent, square corners, hairlines, no
shadows, and nothing moving at all.

A narrow column of prose, left-aligned, on the ivory page. At the top, the house
name as a small widely-tracked bronze eyebrow. Beneath it, the page title in a
large display serif. Beneath that, a small muted line giving the month and year
the page was last updated.

To the right of the title on a wide screen, a short scannable pair of facts: two
label-and-value rows, one giving the return window for unopened items and one
giving the typical dispatch window, each label in small tracked capitals in warm
grey and each value in full ink. On a phone these two rows sit below the title.
This pair exists because it is the only thing most visitors come to read.

Then four or five sections, separated by generous space, each with a display serif
subheading and one or two paragraphs beneath in warm grey at a narrow measure with
comfortable leading. The prose is plain and factual. Where a customer must email
something, the email address is set in full ink with a plain underline. On a wide
screen the page becomes two columns: a small table of contents of plain in-page
links on the left, and the prose on the right. The table of contents does not
highlight which section you are reading.

At the bottom, a hairline rule, then a small muted block with a line about
contacting support, the support email as an underlined link, a note about reply
time, and plain underlined cross-links to the terms and the privacy policy.

Accent motion: none. Nothing on this page animates, at any breakpoint, including
on first load. The prose does not fade in.

Avoid: accordions that animate open, an expand-all control, a scroll progress
indicator at the side, a table of contents that highlights the current section, a
print or download button, a share button, a "was this article helpful" widget, a
floating contact button, coloured callout boxes with icons, gradients, glass, and
drop shadows.
```

---

### 3.10 — `/login` and `/register` — account

- **Job:** get someone signed in or created, and nothing else.
- **Sections, in order:** the house wordmark in the display serif, centred, above
  a page title in the same serif; then the form; then a hairline and a small block
  of secondary links.
- **`/login`.** Two labelled rectangular fields — username, password — then one
  full-width solid dark button reading "Sign in". **There is no third field.** No
  one-time-code input on the customer door; a customer authenticates with a
  username and a password and offering a third factor there is noise they cannot
  evaluate. Beneath a hairline, a small muted block of plain underlined links:
  create an account, resend a verification email, forgot your username, forgot
  your password, and a staff sign-in link. **All of them are always visible,
  whether or not sign-in failed** — the failure message is deliberately the same
  for a wrong password and an unverified account, so the links must not be
  conditional on the reason.
- **`/register`.** Three labelled fields — username, email, password — then one
  full-width solid dark button. Beneath a hairline, a small muted line linking
  back to sign in.
- **Accent moment:** **none.** A form that fades in is a form that suggests its
  fields were not always there.
- **Responsive notes:** phone and desktop are the same — a single narrow centred
  column, at most twenty-eight rem wide, with generous space above. There is
  nothing to reflow, and that is the correct outcome for a form this short.
- **Negative direction:** no social sign-in buttons (no provider exists), no
  "remember me" (the session is server-owned), no CAPTCHA (none is required and
  none would work), no password-strength meter, no show-password toggle on a
  premium account surface, no inline illustration beside the form, no
  three-column marketing panel, no gradients.
- **Spine:** §1's field styling verbatim — the same rectangle that appears in the
  cart, the checkout and the recovery pages.

```
Sign-in and account-creation pages for a perfume house. Warm ivory, deep ink, one
bronze accent, square corners, hairline-bordered fields, no shadows, nothing
moving.

A single narrow column, no wider than about twenty-eight rem, centred on the ivory
page with a great deal of empty space above it. At the top the house wordmark in
the display serif, centred. Beneath it, a page title in the same serif.

On the sign-in page, two stacked fields, each with a small label above a plain
hairline-bordered rectangle of the same width: username, then password. The
password field is a proper password field. Beneath them, one full-width solid
deep-ink button reading Sign in.

Beneath the button, a hairline rule, then a small block of plain underlined links
in warm grey: an invitation to create an account, a link to resend a verification
email, forgot your username, forgot your password, and a staff sign-in link. All
of these links are always present, whether or not the sign-in attempt succeeded,
and none of them appears or disappears based on the error.

On the account-creation page, the same layout with three stacked fields —
username, email, password — and one full-width solid deep-ink button.

Accent motion: none. Nothing on these pages fades in, slides, or reveals. A form
that animates suggests its fields might not have been there.

Avoid: social sign-in buttons, a remember-me checkbox, a CAPTCHA, a password
strength meter, a show-password toggle, an illustration or photograph beside the
form, a three-column marketing panel next to the form, coloured error banners,
inline icons in fields, progress indicators, gradients, glass panels, rounded
fields, and drop shadows on the form.
```

---

### 3.11 — `/forgot-password`, `/forgot-username`, `/reset-password`, `/verify-email`, `/resend-verification` — recovery and verification

- **Job:** be unmissable and quiet for someone who is locked out.
- **Sections, in order:** the house wordmark, a page title in the display serif,
  then either one labelled field and one full-width button, or — after submitting —
  a single hairline-outlined panel containing one short sentence.
- **The shared rule that matters more than the layout.** These pages must give the
  same answer whether or not the account exists. The panel's wording is whatever
  the server says, shown word for word. **No second message, no "we couldn't find
  that account", no different tone for a miss, no link that appears only on
  failure.** Making the two paths visibly different would turn a recovery form
  into a way to discover which email addresses have accounts.
- **`/reset-password`** adds a second field for the new password, and renders the
  server's password-policy messages one per line beneath the field rather than as
  a single blob.
- **`/verify-email`** runs its check on arrival and shows one of two panels: it
  worked, or the link is no longer valid — with a plain link to request a new one.
- **Accent moment:** **none, by rule.** These pages are used by someone who is
  locked out. They get the quietest treatment in the system.
- **Responsive notes:** identical at every breakpoint — one narrow centred column.
- **Negative direction:** no progress indicator, no countdown to expiry, no
  "we'll email you in a moment" animation, no confetti on success, no celebratory
  illustration, no password strength meter, no differing states.
- **Spine:** §1, the quietest application of it.

```
Credential-recovery and email-verification pages for a perfume house, all sharing
one layout. Warm ivory, deep ink, one bronze accent, square corners,
hairline-bordered fields, no shadows, absolutely nothing moving.

A single narrow column, about twenty-eight rem, centred on the ivory page with
generous empty space above. The house wordmark in the display serif at the top,
then a page title in the same serif.

One labelled field — a single rectangular hairline-bordered input for an email
address or a username, depending on the page — then one full-width solid deep-ink
button. After the button is used, the form is replaced by one plain rectangular
panel outlined with a single hairline, containing one short calm sentence
confirming what was done.

The wording of that panel must be identical whether or not an account exists, and
whether or not the address was correct. There is no second message, no "we could
not find that account", no success-versus-failure styling, no link that appears
only when something went wrong, and no change of tone. The page must give away
nothing about whether an account exists.

On the password-reset page, one panel shows the result of the check that arrived
from the emailed link: either that the address is confirmed, or that the link is no
longer valid, with a plain underlined link to request a fresh one.

On the password-set page, two stacked fields — a new password and its
confirmation — and the server's own password policy messages appear one per line
directly beneath the field rather than as a single combined sentence.

Accent motion: none. Nothing on these pages animates in any circumstance.

Avoid: a countdown timer to link expiry, a spinner, a "check your inbox" animation
of any kind, confetti, a celebration illustration, a success-versus-error
difference in wording or styling, a password strength meter, coloured banners,
gradients, glass, rounded fields, and drop shadows.
```

---

### 3.12 — `/contact` — contact

- **Job:** give every real way to reach the house, and hide the ones that are not
  configured.
- **Sections, in order:** the house name as a small bronze eyebrow, a page title
  in the display serif, then four short sections — reach us directly, customer
  support, payments, and a short paragraph about the house.
- **The channel row** is four square outlined buttons in a row, each holding one
  simple line-drawn glyph: a chat bubble, a handset, an envelope, and a camera
  outline. Each glyph is a plain single-weight outline that inherits the text
  colour and darkens to the accent on hover. **No emoji, no filled icons, no
  brand-coloured app logos, no circular buttons.** If a channel is not configured
  for the store, its button simply is not rendered — there is no disabled button
  and no dead link.
- **Accent moment:** **none.** Four channels cannot be sequenced.
- **Responsive notes:** phone — the channel buttons stay in one row of four if
  they fit at touch size, otherwise wrap to two rows of two. Desktop — a row of
  four at the left of the section.
- **Negative direction:** **no contact form.** There is no endpoint for one, and a
  form that submits nowhere would collect a customer's details and discard them.
  Also: no map embed, no office photograph, no office hours table, no live-chat
  widget, no social feed, no newsletter signup on this page.
- **Spine:** §1's hairline squares; the glyphs are 24-pixel line drawings at the
  same weight as the interface's rules.

```
A contact page for a perfume house. Warm ivory, deep ink, one bronze accent,
square corners, hairline rules, no shadows.

The house name as a small widely-tracked bronze eyebrow at the top, then a page
title in a large display serif reading Contact.

Below, four short sections separated by generous space, each with a small display
serif subheading and one or two paragraphs of quiet warm-grey prose at a narrow
measure.

The first section, about reaching the house directly, contains a single row of
four small square buttons outlined with one hairline each, holding one simple
single-weight line-drawn glyph apiece: a speech bubble, a telephone handset, an
envelope, and a camera outline. The glyphs are plain outlines in the same ink as
the text, inheriting the text colour, and darkening to the bronze accent on hover.
No emoji anywhere, no filled or solid icons, no brand-coloured app logos, and the
buttons are square, not circular. If a channel is not configured for this store,
its button is simply absent — there is no greyed-out button and no dead link.

The second section explains that for anything about an order the fastest answer
includes the order reference, and gives the support email address set in full ink
with a plain underline.

The third section is a short paragraph about payment problems and refunds, naming
the payment provider in plain text.

The fourth section is a short factual paragraph about the house.

Accent motion: none. Nothing on this page animates.

Avoid: a contact form, since there is nowhere for one to submit, a map embed, an
office or studio photograph, an opening hours table, a live chat widget, an
embedded social feed, a newsletter signup, emoji as icons, filled or coloured app
icons, circular icon buttons, coloured banner backgrounds, gradients, glass, and
drop shadows.
```

---

### 3.13 — `/terms` and `/privacy` — legal

- **Job:** be complete, be readable, and be unmistakably itself.

- **Sections, in order:** identical to the policy layout in 3.9 — bronze eyebrow,
  page title in the display serif, a muted "last updated" line, then five to seven
  sections each with a display serif subheading and prose at a narrow measure,
  then a hairline and a closing block with the support address and cross-links.
- **One addition over the policy pages:** a narrow table of contents in a left
  column on a desktop, because these are the two longest pages on the site. It is
  a plain list of in-page links that **does not highlight the current section**.
- **Prose treatment:** defined terms inside a paragraph are set in full ink rather
  than the muted grey of the surrounding prose, so a reader can find what is being
  promised without hunting. There are no inline icons, no callout boxes, no
  highlighted blocks.
- **Accent moment:** **none.**
- **Responsive notes:** phone — one column, **no table of contents** (a seven-item
  contents on a phone is worse than none). Desktop — two columns as above.
- **Negative direction:** no accordions, no "expand all", no scroll progress bar,
  no highlighted current section, no print or download button, no share button, no
  cookie-consent banner (there are no analytics or trackers to consent to, and
  claiming otherwise would be false), no "last updated" auto-refresh.
- **Spine:** §1, identical to 3.9.

```
Terms and privacy pages for a perfume house, sharing one layout. Warm ivory, deep
ink, one bronze accent, square corners, hairlines, no shadows, nothing moving.

A narrow column of prose, left-aligned. The house name as a small widely-tracked
bronze eyebrow, then the page title in a large display serif, then a small muted
line giving the month and year the page was last updated.

On a wide screen the page is two columns: a small table of contents of plain
in-page links in a narrow left column, and the prose in a wider right column. The
table of contents does not indicate which section is being read. On a phone the
table of contents is not shown at all and the prose is a single column.

Then five to seven sections separated by generous space, each with a display serif
subheading and paragraphs beneath in warm grey at a narrow measure with comfortable
leading. Where a specific defined term appears inside a paragraph, that term alone
is set in full ink rather than the muted grey of the surrounding prose, so the
promises being made can be found without hunting. Prose is continuous and
unbroken; there are no lists broken into boxes, no icons, no highlighted callouts,
and no expandable sections.

At the bottom, a hairline, then a small muted block with a line about questions, the
support email as an underlined link, a note about reply time, and plain underlined
cross-links to the other policy pages.

Accent motion: none. Nothing on these pages animates, including on first load.

Avoid: accordions that animate, an expand-all control, a reading progress bar, a
table of contents that highlights the current section, a print or download button,
a share button, a cookie consent banner when there are no trackers to consent to,
coloured callout boxes with icons, icon bullets, gradients, glass, and drop
shadows.
```

---

### 3.14 — not-found and error

- **Job:** turn a dead end into a way back.

- **Sections, in order:** a small bronze eyebrow reading either a four-zero-four or
  that something went wrong; a page title in the display serif; one muted
  paragraph explaining what happened in plain, non-blaming language; then, on the
  not-found page, a row of four plain underlined links — all fragrances, new
  arrivals, contact, and home. On the error page, one solid dark "try again"
  button and, beneath it, a small hairline-outlined row showing a short reference
  code the customer can quote to support.
- **Accent moment:** **none.** An error page that animates looks like a page that
  is still loading, which makes the customer's situation worse.
- **Responsive notes:** identical at every breakpoint — a narrow centred block,
  generous space above, nothing to reflow.
- **Negative direction:** no illustration, no empty-state drawing, no mascot, no
  apology carousel, no "here are some products you might like", no search box, no
  confetti, no animated gradient.
- **Spine:** §1, at its plainest.

```
Not-found and error pages for a perfume house. Warm ivory, deep ink, one bronze
accent, square corners, hairlines, no shadows, nothing moving.

A narrow centred block with a great deal of empty space above it. At the top a
small widely-tracked bronze eyebrow. On the not-found page the eyebrow reads four
zero four and the page title below it, in a large display serif, reads that this
page does not exist. Beneath the title, one short paragraph in warm grey explaining
in plain, un-blaming language that the link may be outdated or the product may
have been retired.

On the not-found page, beneath the paragraph, a row of four plain underlined links
in the sans, separated by generous gaps: all fragrances, new arrivals, contact, and
home. Four real destinations, so that a wrong address becomes a way back into the
store rather than a dead end.

On the error page the eyebrow reads that something went wrong and the title reads
that the page could not be loaded, with one short paragraph suggesting trying again
or returning shortly. Beneath that, one solid deep-ink button reading Try again.
Beneath the button, a single hairline-outlined rectangular row containing a small
muted label and a short reference code in evenly spaced digits, introduced by a
line explaining that this code can be quoted to support.

Accent motion: none. Nothing on either page animates. An error page that moves
looks like a page that is still loading, which makes the visitor's situation worse.

Avoid: illustrations, empty-state drawings, mascots, apology carousels, a
"products you might like" strip, an embedded search box, confetti, animated
gradients, coloured backgrounds, rounded panels, and drop shadows.
```

---

## 4. Researcher completion notes

Record here, briefly:

- Which brand URLs resolved, and which needed the alternate spelling.
- Which prompts needed more than one iteration, and what had to change.
- Any page where the design-system prompt was insufficient and had to be restated
  locally.

Keep this under 20 lines. It is provenance, not a report.

### Provenance — research, 2026-10-02

**Brand URLs.**

| URL | Result |
|---|---|
| `https://celestialperfume.in/` | **resolved.** Shopify store |
| `https://celestialbeauty.in/` | not attempted (the first resolved) |
| `https://fraganote.com/` | **resolved.** Shopify store |
| `https://fragnote.in/` | not attempted (the first resolved) |
| `https://bellavitaorganic.com` | **resolved** |
| `https://perfumeoilcorner.com` | **resolved.** Shopify store |
| `https://ajmalperfume.com` | **403** — bot-blocked. `www.ajmalperfume.com` also **403** |
| `https://www.uffco.com/` | **resolved.** Shopify store |
| `https://ufffragrances.com/` | not attempted (the first resolved) |
| `https://land-book.com` · `https://godly.website` | both **resolved** (chrome only — no single reference was extracted from either; they were used as confirmation that the "Motion" and "Parallax" categories are where this category's reference design lives) |

**Prompts iterated:** none. **No Stitch prompt in this file has been run** — there
is no Stitch API and no MCP, this is a human-in-the-loop step, and §13 A-7 of the
spec records this as an open assumption. Nothing here depends on a Stitch output
existing; the spec is executable from words alone.

**Where the design-system prompt had to be restated locally:** in every page
prompt, deliberately. §1's rules are restated per page — palette, the square
corner language, the hairline-not-shadow surface treatment, and the negative list —
because Stitch does not reliably carry context between generations. Two pages
needed one addition §1 does not contain: `/checkout` and `/orders/[id]` each
needed an explicit "this must not animate" clause for their money states, and
`/track-order` needed the uniformity constraint written into the prompt body
because it is the single most important thing on that page.

**One correction made to the worked example, recorded in §3.2's header:** the
shipped `products/[slug]` block specifies a slow scale-and-settle on the gallery
image. The gallery image is the largest painted element on that page, and the
spec overrides it — the entry motion moves to the purchase column and the
scroll-linked drift applies only below the fold. The worked example is left in
place as the density reference; the override is stated above it so neither the
architect nor a future researcher misses the conflict.
