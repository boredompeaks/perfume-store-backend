# Google Stitch prompt file — perfume-store storefront

> **STATUS: SKELETON.** This file ships the convention and the worked examples.
> `design-research` fills every `TODO` page block. An unfilled `TODO` is a
> dispatch blocker — the UI orchestrator will not proceed to implementation
> while any remain.
>
> Owner: `design-research`. Consumers: the user (drives Stitch by hand) and
> `frontend-architect` (executes the resulting design).

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

> **TODO — `design-research` writes the real prompt.**
> It must define, in one generation: the palette, the type pairing, the type
> scale, the spacing rhythm, the corner language, surface/elevation treatment,
> the motion character (durations and easings by intent), the imagery direction
> (lighting, background, crop, grade), and a negative-direction list.

**Direction to encode** (from `UI-UX-SPEC.md` §2; do not contradict it):

- restrained-luxury base, theatrical motion as accent
- CSS-first: the motion character must be expressible with `transform`/`opacity`
  so it can ship without a JS dependency

---

## 2. Coverage contract

`design-research` MUST emit one block per row. `frontend-architect` implements
per block, so a missing row is a page nobody builds.

| # | Route | Page job (one line) | Accent moment | Status |
|---|---|---|---|---|
| 1 | `/` | Brand statement and entry to the catalogue | TODO | TODO |
| 2 | `/products` | Product listing — browse and filter | TODO | TODO |
| 3 | `/products/[slug]` | Product detail — the money page | TODO | TODO |
| 4 | `/cart` | Cart review and quantity edit | TODO | TODO |
| 5 | `/checkout` | Address, shipping, payment | TODO | TODO |
| 6 | `/orders` | Order history | TODO | TODO |
| 7 | `/orders/[id]` | Single order + live tracking | TODO | TODO |
| 8 | `/returns` | Return request and status | TODO | TODO |
| 9 | `/shipping` | Shipping policy | TODO | TODO |
| 10 | `/login` | Sign in | TODO | TODO |
| 11 | `/register` | Account creation | TODO | TODO |
| 12 | `/forgot-password` · `/forgot-username` · `/reset-password` | Credential recovery | — (quiet by design) | TODO |
| 13 | `/verify-email` · `/resend-verification` | Verification | — (quiet by design) | TODO |
| 14 | `/contact` | Contact and enquiry | TODO | TODO |
| 15 | `/terms` · `/privacy` | Legal | — (quiet by design) | TODO |

**Explicitly out of scope for this redesign** — `design-research` must state the
decision and the reason, not leave it silent:

- `/admin` and `/staff/*` — the admin/staff surfaces are operational tools, not
  brand surfaces. Redesigning them for a luxury storefront brief is the wrong
  call; they keep the existing system. State this in the spec.

---

## 3. Per-page blocks

Each block uses this shape. **The `products/[slug]` block below is the worked
example** — it shows the expected density and specificity.

### 3.x — `<route>` — TODO status

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

---

## 4. Researcher completion notes

Record here, briefly:

- Which brand URLs resolved, and which needed the alternate spelling.
- Which prompts needed more than one iteration, and what had to change.
- Any page where the design-system prompt was insufficient and had to be restated
  locally.

Keep this under 20 lines. It is provenance, not a report.
