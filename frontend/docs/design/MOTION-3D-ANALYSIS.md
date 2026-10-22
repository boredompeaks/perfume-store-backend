# 3D and scroll-motion feasibility analysis

> **STATUS: analysis, 2026-10-03.** Owner: `design-research`. Consumers: the
> user (a stakeholder asked for a "3D cube assembles as you scroll" effect) and
> `frontend-architect`.
>
> **This file does not change the design.** `UI-UX-SPEC.md` §3, §5, §10 and §11
> remain the contract. This is the evidence behind one decision and the
> corrections that evidence forced. Where the two files disagree, the correction
> is stated here *and* applied to `UI-UX-SPEC.md` — see §7.
>
> **No application code, no dependency, no config change.** `package.json` is
> untouched. Nothing was installed. §7.4 records what I could not verify.

---

## 0. Method, and what each number means

Three different kinds of number appear in this file and they are **not
interchangeable**. Mixing them is the most common way a performance argument goes
wrong, so the categories are defined first.

| Category | Definition | How produced here | Trust |
|---|---|---|---|
| **M — measured by me** | A number I retrieved and can reproduce | `curl.exe` with `Accept-Encoding: gzip`, `size_download` = bytes on the wire. Package builds downloaded from unpkg and gzipped **locally** with .NET `GZipStream(Optimal)`. Registry metadata via `npm view`. | High, with the limits in §0.1 |
| **O — observed** | A fact read out of source I retrieved (a string, a tag, a count) | Regex/fingerprint over the downloaded bytes | High for *presence/absence*; see §0.1 |
| **E — estimate** | An order-of-magnitude figure from general knowledge | Reasoning, not retrieval | Low. Marked wherever used |

### 0.1 The limits of my own data — read this before quoting any figure

1. **One machine, one network, one moment.** All site measurements come from a
   single Windows host on a single connection with a desktop Chrome user-agent.
   They are **comparative evidence between sites measured identically**, not
   absolute performance. They are not Lighthouse scores and they are not Core Web
   Vitals. I did not run Lighthouse, WebPageTest or DevTools on any of them.
2. **Server-compressed transfer size is not "first-load JS".** When I report
   "script gz", I mean *the sum of the gzip transfer sizes of `<script src>`
   resources reachable from the delivered HTML*. For a Shopify storefront that sum
   includes jQuery, marketing popups, chatbots, review widgets, video apps and
   Google Tag Manager — none of which a restrained luxury storefront would ship.
   **It is not the cost of animation**, and comparing it to our 120 KB JS budget is
   a category error. I make that comparison explicitly in §2.4 only to make a
   bounded point about authored weight, and I label it there.
3. **Some scripts are below the fold or app-gated and I could not tell.**
   Without a rendering browser I cannot distinguish "loaded eagerly" from "loaded
   on interaction", nor measure request priority, nor observe the main thread.
4. **Unminified package builds are not what a bundler ships.** `three@0.186.1`'s
   published `build/three.module.js` is unminified ESM. A real build
   tree-shakes and minifies. **I did not run a bundler**, so my three.js figure is
   a ceiling, not a realistic production figure. §3.1 handles this honestly by
   triangulating against Google's own Closure-compiled production bundle.
5. **The CodeWiki cube: I established the renderer, not the visual.** I
   retrieved the page shell, both JavaScript bundles and the stylesheet, and I can
   state with confidence **what technology is present and what is absent**. I
   cannot confirm I have *seen* the specific scroll-assembled cube the stakeholder
   described. §2 states exactly this and does not overreach.
6. **`prefers-reduced-motion` counts are presence counts, not conformance.**
   `reducedMotion=6` means the string appears six times. It does not mean six
   correct rules, nor that they cover the hero animation.

---

## 1. Executive answer

| Question | Answer |
|---|---|
| What does the Google cube use? | **three.js / WebGL**, bundled. Measured **213.4 KB gz** for the bundle containing it. CSS 3D is **absent**: `preserve-3d`, `matrix3d`, `rotate3d`, `animation-timeline`, `view-timeline` all return **0** across both bundles and the stylesheet. |
| What do the five reference perfume sites use? | **No 3D of any kind, and no animation library.** Shopify platform + Swiper carousels + jQuery + video + marketing apps. Their hero motion is CSS keyframes and video. |
| Does the CSS 3D version fit our budget? | **Yes — 0 KB.** But bytes are not the cost centre. |
| What is the CSS 3D version's real cost? | **Composited layer count and GPU memory**, not download. Six faces in a `preserve-3d` context is ~6 render surfaces; at 800×800 RGBA that is ~**15.7 MB of GPU memory** before depth, stencil or mipmaps, which is the figure that matters on a low-end phone. |
| Is it viable? | **Marginally, and only below the fold, and only on desktop.** My recommendation is **no**. Cheapest alternative in §5.4. |
| Is the spec's CSP argument against GSAP valid? | **No — and the stakeholder is right.** See §7.1. The CSP blocks *third-party CDN* scripts; a bundled npm dependency is same-origin and satisfies `'self'`. The 120 KB budget is the only real constraint. |

---

## 2. What the reference sites actually load

### 2.1 Delivery and weight — all M except the JS sums, which are lower bounds

`curl.exe -s -L -A "<desktop Chrome UA>"`, `Accept-Encoding: gzip`, so
`size_download` is bytes on the wire. HTML gz recomputed locally to confirm.

| Site | HTTP | HTML raw | HTML gz | `<script src>` | CSS link tags | `<video>` | `<canvas>` |
|---|---|---|---|---|---|---|---|
| `celestialperfume.in` | 200 | 452,101 | **71.7 KB** | 32 | 86 | 1 | 0 |
| `fraganote.com` | 200 | 1,598,754 | **190.1 KB** | 42 | 441 | 0 | 0 |
| `bellavitaorganic.com` | 200 | 1,438,926 | **171.2 KB** | 31 | 71 | 0 | 0 |
| `perfumeoilcorner.com` | 200 | 2,281,999 | **202.4 KB** | 25 | 16 | 6 | 0 |
| `uffco.com` | 200 | 803,184 | **96.2 KB** | 37 | 34 | 0 | 0 |
| `land-book.com` | 200 | 339,554 | **27.5 KB** | — | — | — | — |
| `godly.website` | 200 | 306,063 | **36.6 KB** | — | — | — | — |

**Two of the five deliver 1.4–2.3 MB of HTML.** `fraganote.com` ships 441
stylesheet/preload link tags. `perfumeoilcorner.com` carries 199 **inline**
`<script>` blocks. These are Shopify platforms rendering a great deal of
first-party data into the document.

### 2.2 Script weight — a lower bound, and why

The first pass measured only `src` attributes beginning `http`. Shopify emits
many scripts as **protocol-relative** `//cdn.shopify.com/…`, which that filter
excluded. Both passes, and the combined figure:

| Site | Absolute `src` (n) | gz | Protocol-relative (n) | gz | **Combined gz** |
|---|---|---|---|---|---|
| `bellavitaorganic.com` | 9 | 67.1 KB | 21 | 71.6 KB | **138.7 KB** |
| `celestialperfume.in` | 14 | 464.9 KB | 15 | 23.3 KB | **488.2 KB** |
| `fraganote.com` | 17 | 320.0 KB | 18 | 30.8 KB | **350.8 KB** |
| `perfumeoilcorner.com` | 10 | 70.7 KB | 14 | 29.0 KB | **99.7 KB** |
| `uffco.com` | 14 | 563.5 KB | 18 | 71.3 KB | **634.8 KB** |

**Still a lower bound.** Excluded: every inline `<script>` body, any script
injected at runtime that is not in the delivered HTML, and lazy chunks behind app
gates. `celestialperfume.in`'s largest single script is `js?id=G-DTV2QSHN7N` at
**194.6 KB gz** — a Google tag-manager payload. `uffco.com` carries
`seal-review.min.js` at 107.8 KB, `chatbot.js` at 88.1 KB, `reelfy.js` (a video app)
at 81.1 KB, and another tag-manager payload at 164.4 KB.

### 2.3 The fingerprint — and three false positives worth recording

| Site | Libraries/tech found (O) |
|---|---|
| `bellavitaorganic.com` | Shopify, Google Tag Manager, Meta Pixel, video assets |
| `celestialperfume.in` | Shopify, **Swiper**, jQuery 3.7.1, `backdrop-filter`, Google Fonts payload, merchant apps, announcement-bar app |
| `fraganote.com` | Shopify, **Swiper**, jQuery, **"WebGL"**, marquee, video-shopping app, bitespeed popups, globo preorder, chatbot |
| `perfumeoilcorner.com` | Shopify, **Swiper**, `backdrop-filter`, `will-change`, 6 videos |
| `uffco.com` | Shopify, **Swiper**, jQuery, `matrix3d`/`rotate3d`, marquee, `will-change`, seal reviews, chatbot |
| `land-book.com` | "Framer" |
| `godly.website` | "motion" |

**Absent from every single one of the seven:** `three.js`, GSAP, ScrollTrigger,
Lenis, Locomotive Scroll, Framer Motion / `motion-dom`, `@model-viewer`, CSS
`animation-timeline`, `view-timeline`, `scroll-timeline`.

Three hits were false positives, and each was checked rather than reported:

- **`fraganote.com` "WebGL"** is an **anti-bot device-fingerprinting probe**. It
  creates a throwaway canvas, reads `UNMASKED_VENDOR_WEBGL`,
  `UNMASKED_RENDERER_WEBGL`, `SHADING_LANGUAGE_VERSION`, `MAX_TEXTURE_SIZE`,
  `MAX_VERTEX_ATTRIBS`, the full sorted extension list, shader precision formats
  and `hardwareConcurrency`, then calls `WEBGL_lose_context`. It renders nothing.
  Worth knowing for a different reason: that is GPU-fingerprinting code on a
  perfume storefront, and it is not something a restrained house would ship.
- **`uffco.com` `matrix3d`/`rotate3d`** is a **flat spinner**: the keyframes are
  `rotate3d(0, 0, 1, 0deg→90deg→180deg→270deg→360deg)`. Axis `(0,0,1)` is the
  screen normal, so that is a 2D rotation expressed in 3D matrix syntax. There is
  no 3D geometry, no `perspective`, no `preserve-3d`.
- **`land-book.com` "Framer"** is `"name":"Framer Template"` inside JSON-LD. They
  are a gallery that *sells* Framer templates.

### 2.4 Reduced motion — the finding that matters most for us

| Site | `prefers-reduced-motion` occurrences (O) |
|---|---|
| `celestialperfume.in` | **0** |
| `fraganote.com` | **0** |
| `bellavitaorganic.com` | **0** |
| `uffco.com` | **0** |
| `perfumeoilcorner.com` | 6 |
| `land-book.com` / `godly.website` | 0 |

**Four of the five category leaders ship no reduced-motion handling at all** —
while shipping infinite marquees (§2.5) and carousel autoplay. This is the single
most useful thing in this section, and it is not about 3D.

It also means the reference set provides **no example of a well-implemented 3D
effect** — by construction. If the house wants 3D, it will be inventing the
category's precedent, not following it.

### 2.5 What the category actually does instead of 3D

Observed motion in the five sites, in order of how often it appears:

1. **Infinite marquee** — `fraganote.com` repeats one discount string ten times
   in the delivered HTML; `uffco.com` repeats a four-part offer string four
   times. Fails WCAG 2.2.2 Pause/Stop/Hide (AA) and, per §2.4, with no opt-out.
2. **Swiper carousels** on product and editorial rails.
3. **Video** — `perfumeoilcorner.com` ships six `<video>` elements;
   `bellavitaorganic.com` and `fraganote.com` reference video assets.
4. **`backdrop-filter`** frost on `celestialperfume.in` and
   `perfumeoilcorner.com`.
5. **`will-change`** hints on `perfumeoilcorner.com` and `uffco.com`.

**None of these is a scroll-scrubbed 3D effect, and none is a scroll-linked 2D
effect either.** The category reaches for 3D-ness with *video and autoplay*, not
with geometry.

### 2.6 The bounded point this evidence supports

`perfumeoilcorner.com` is the one site with both reduced-motion handling **and**
`will-change` discipline, and it is also the only one under 120 KB of scripts
(**99.7 KB gz**). `celestialperfume.in` at **488.2 KB gz** and `uffco.com` at
**634.8 KB gz** ship the loudest motion and the heaviest third-party stack.

Stated carefully: **within this category, authored script weight does not buy
motion quality — it buys popups, chatbots, review widgets and tag managers.**
That is a statement about *what the bytes are spent on*, not a Lighthouse
comparison, and not a finding that a 99.7 KB site scores better than a 634.8 KB
one. I did not measure either.

---

## 3. What Google's cube actually is

### 3.1 Resolution, and exactly what I could and could not establish

**Resolved:** `https://codewiki.google/` — HTTP 200, Google's code-documentation
surface. Title `Code Wiki`; the app identifies itself as
`BoqAngularSdlcAgentsUi` (a Closure-compiled Angular app), with a
`<sdlc-agents-root>` custom element and an `AF_initDataCallback` Closure
bootstrap. A second, older-looking codelabs URL I tried first returned 404 and I
discarded it rather than substitute a different site.

**Established (O), with high confidence, from the delivered bytes:**

| Asset | Reference | Raw | **gz (M)** |
|---|---|---|---|
| Base JS | `<script id="base-js">` → `gstatic.com` | 852,952 | **291.2 KB** |
| **Bundle containing three.js** | `<link rel="preload">` → `gstatic.com` | 831,522 | **213.4 KB** |
| Stylesheet | `<link rel="stylesheet">` → `gstatic.com` | 30,274 | **5.6 KB** |
| HTML shell | — | 77,316 (as retrieved) | — |

**Total JavaScript for one page: ≈ 504.6 KB gz.** That is **4.2× our entire
120 KB first-load budget**, before a single line of our own code.

**What I did NOT establish.** I did not see the effect rendered. I cannot confirm
that the cube the stakeholder described is on the codewiki.google **landing page**
as opposed to a project or docs view inside the product, and I cannot observe the
scroll behaviour, the scrub range, or whether the cube is above or below the fold.
**Everything below is about the technology present in the bundles, which is a fact
I can prove, and not about a specific animation I could not see.** If the effect
turns out to live on a route I did not retrieve, the technology identification
still holds — three.js is three.js — but the load-behaviour detail in §3.4 would
need re-checking.

### 3.2 It is three.js, and it is emphatically not CSS 3D

Counts across **both** bundles and the stylesheet:

| Token | base.js | three bundle | stylesheet |
|---|---|---|---|
| `THREE.` | 0 | **196** | — |
| `webgl` / `WebGL` | 0 | **340** | — |
| `shader` | 0 | **229** | — |
| `getContext` | 0 | **15** | — |
| `drawArrays` / `drawElements` | 0 | 4 / 4 | — |
| **`preserve-3d`** | **0** | **0** | **0** |
| **`matrix3d` / `rotate3d`** | **0** | **0** | **0** |
| **`animation-timeline` / `view-timeline` / `scroll-timeline`** | **0** | **0** | **0** |
| `rotateX` / `rotateY` / `perspective(` | 0 | 2 / 0 / 0 | 0 |
| `requestAnimationFrame` | 14 | 5 | — |
| `prefers-reduced-motion` | **3** | **0** | **0** |

three.js is unmistakable from the class surface alone:
`WebGLRenderer` ×47, `PerspectiveCamera` ×10, `OrthographicCamera` ×8,
`BufferGeometry` ×31, `InstancedMesh` ×16, `ShaderMaterial` ×29,
`RawShaderMaterial` ×17, `MeshStandardMaterial` ×14, `PMREM` ×11,
`Raycaster` ×3, `TextureLoader` ×7, `VideoTexture` ×12, `FogExp2` ×6,
`SRGBColorSpace` ×5, `ACESFilmicToneMapping` ×2, plus `BoxGeometry`,
`TorusKnot`, `ExtrudeGeometry`, `LatheGeometry`, `IcosahedronGeometry`,
`TubeGeometry`, `CatmullRomCurve`.

That is **the whole of three.js, not a subset.** It is a full renderer with an
environment-map prefilter, tone mapping, a raycaster and even video textures.

**The four "three" hits in `base.js` are Unicode glyph names** — `⨳` as
`lthree`/`rthree`/`leftthreetimes` in an Angular math-symbols table. Not three.js.
I checked them rather than reporting them.

**Conclusion: the cube is a `three.js` WebGL scene — option C, the expensive one.**

### 3.3 A full WebGL render loop, not a scrub

This is the important architectural point, and it is provable from the counts.

A scroll-driven effect *can* be declarative in WebGL — read a scroll offset in the
render loop and map it to rotation. But it is **still a render loop**: WebGL has no
`animation-timeline` equivalent. The scene must be re-drawn whenever anything
changes, which means the browser runs `requestAnimationFrame` for as long as the
effect is on screen.

The bundles contain **19 `requestAnimationFrame` call sites** across base and
three. A declarative CSS cube needs **zero** of them.

Per-frame cost, then, is a continuous cost:

- `WebGLRenderer.render(scene, camera)` each frame: matrix updates, uniform
  uploads, `drawElements`/`drawArrays` calls, and a full render-surface resolve.
- `preserve-3d` is irrelevant here — the whole viewport becomes a GPU render
  target. On a 390×844 phone at `devicePixelRatio` 3, the drawing buffer is
  **1170×2532 ≈ 2.96 M pixels**; as RGBA that is **~11.3 MB re-cleared and
  re-resolved per frame**, before any geometry is drawn. At 60fps that is a
  sustained ~680 MB/s of fill traffic for a decorative effect.
- `devicePixelRatio` must be **clamped** (typically to 1.5–2) or the fill cost
  scales with the phone's resolution tier — the exact axis that separates a
  low-end device from a laptop.

### 3.4 When it loads — the part that breaks the budget

The three.js bundle is referenced as **`<link rel="preload">`**, not
`<script>` and not `<link rel="modulepreload">`:

```html
<link rel="preload" href="https://www.gstatic.com/…/m=b96Bdf?wli=BoqAngularSdlcAgentsUi…">
```

Consequences:

1. **213.4 KB gz starts downloading during the landing page's load**, competing
   with the two render-blocking stylesheets and the Google Fonts CSS. `preload`
   is a strong hint precisely because the resource is expected to be needed.
2. Execution is deferred to a dynamic module request, so the cube is not
   necessarily *executed* at first paint — but the bytes are already on the
   critical path of the network. Bandwidth is spent whether or not the customer
   scrolls far enough to see the cube.
3. There is no `IntersectionObserver` string in either bundle, so I cannot show
   any intersection-gated lazy-loading of the module. **I cannot rule it out**;
   the pattern could be minified into an indirect call. Marked as unverified
   rather than absent.

### 3.5 Reduced motion is handled — in the wrong bundle

`prefers-reduced-motion` appears **3 times in `base.js` and 0 times in the
three.js bundle**. Two of the three are Angular Material CDK:

```css
@media(prefers-reduced-motion){.cdk-overlay-backdrop{transition-duration:1ms}}
```

The third is the app's own gate, and it is a good one:

```js
fD = matchMedia("(prefers-reduced-motion)").matches;
return fD ? "reduced-motion" : "enabled";
```

So Google **does** honour reduced motion, and the pattern — a single app-level
`matchMedia` check feeding a state string — is very close to §3's token-layer
policy. The count being zero in the three.js bundle is consistent with that gate
living in the base bundle and being read by the 3D code, but I cannot prove the
data flow without rendering. **It is a reasonable inference, not an observation.**

---

## 4. Package sizes — measured

Registry metadata via `npm view <pkg> version dist.unpackedSize` (M). Shipped
builds downloaded from unpkg and gzipped **locally** with
`GZipStream(CompressionLevel.Optimal)` (M). Versions are current as of
2026-10-03.

| Package | Version | `dist.unpackedSize` | File measured | Raw | **gz (M)** |
|---|---|---|---|---|---|
| **three** | 0.186.1 | 20,443,256 | `build/three.core.js` | 1,458,113 | **288.3 KB** |
| | | | `build/three.module.js` | 662,772 | **131.4 KB** |
| | | | **core + module** | 2,120,885 | **419.7 KB** |
| | | | `build/three.tsl.js` (opt-in only) | 36,854 | 7.7 KB |
| **gsap** | 3.15.0 | 6,258,071 | `dist/gsap.min.js` | 72,927 | **28.3 KB** |
| | | | `dist/ScrollTrigger.min.js` | 44,575 | **18.0 KB** |
| | | | **gsap + ScrollTrigger** | 117,502 | **46.3 KB** |
| **motion** | 14.0.0 | 750,887 | `dist/motion.js` | 143,867 | **46.7 KB** |
| **lenis** | 1.3.26 | 457,528 | `dist/lenis.min.js` | 18,722 | **5.4 KB** |

`dist.unpackedSize` is the **whole npm tarball contents**, including source maps,
TypeScript sources, demos and docs. It is not a bundle size and must never be
quoted as one. It is listed because it is the one figure every registry exposes
without a build.

### 4.1 two.js is unminified — the realistic number is smaller, and here it is

`build/three.module.js` ships **unminified ESM**. A real build tree-shakes and
minifies, so **419.7 KB is a ceiling, not a production figure**. I did not run a
bundler, so I cannot quote the shrunken figure from first principles.

Instead, triangulate against a real production pipeline: Google's Closure-compiled
bundle, which contains a **full** three.js scene with renderer, environment
prefilter, tone mapping and raycaster, and which Closure ADVANCED has renamed and
compressed. That bundle is **213.4 KB gz (M)**.

> **The defensible range for "a real three.js scene in a production build" is
> roughly 150–250 KB gz, anchored by a measured 213.4 KB from Google's own
> pipeline.** The lower half of that range would require a genuinely minimal scene
> with aggressive tree-shaking; the measured 213.4 KB is a *full* library and is
> therefore closer to the pessimistic end.

Either end of that range is **≥ 1.25× our entire 120 KB budget for the animation
alone**, before React, Next, TanStack Query and the app's own code.

### 4.2 `motion`'s mini build — a figure I could not verify

`UI-UX-SPEC.md` §11.3 cites "~2.6–5.5 KB for the `mini` animate build" (A-6, **E**).
`motion@14.0.0`'s `package.json` does declare `./mini` and `./react-mini` entries,
but every path I tried under `mini/dist/` returned **404**, and I did not download
the tarball to read the manifest. **That figure remains an estimate and is now
marked as one.** The full build's measured **46.7 KB gz** is the number I can
stand behind.

### 4.3 Two estimates in the spec that the measurement corrects

| §11.3 claim (A-6, **E**) | Measured | Effect on the conclusion |
|---|---|---|
| GSAP + ScrollTrigger "~31 KB gz" | **46.3 KB gz** | Rejection gets **stronger**: 46.3 / 120 = **38.6%** of the whole budget, not 26% |
| `motion` "~18–34 KB" | **46.7 KB gz** | Rejection gets **stronger**: 46.7 / 120 = **38.9%**, not 15–28% |
| Lenis "~3.5 KB" | **5.4 KB gz** | Rejection gets marginally stronger; still trivially affordable, still rejected on the R4 behavioural grounds, not the byte count |

**Every correction pushes the same way.** The dependency argument in §11 does not
depend on the estimates; it survives them being wrong in the unfavourable
direction. That is the useful property of having stated them as estimates.

---

## 5. The three approaches, costed against our constraints

Our constraints, restated: **120 KB gz first-load budget per route** (§10.2);
**nothing above the fold animates before LCP** (§3, §10.1); **reduced motion is a
token-layer gate with static final state** (§3, §7.6); **at most six scroll-driven
elements per page, `translateY` capped at 6% of viewport height** (§3); **one
accent moment per page** (P2); **mobile is not a smaller desktop**.

### 5.1 Option A — CSS 3D transforms on a scroll timeline

**Technique.** A `perspective` on a container, `transform-style: preserve-3d` on a
wrapper, six absolutely-positioned faces each carrying
`rotateY(0|90|180|270deg) translateZ(half-width)`, and `rotateY()` on the wrapper
driven by `animation-timeline: view()` or `scroll()`.

| | |
|---|---|
| **Mechanism** | CSS-only. `transform` and `opacity` are the only properties that change. |
| **Dependency cost** | **0 KB.** Nothing to install; `package.json` unchanged. |
| **JS per frame** | **Zero.** This is the decisive property: it is **declarative**. No `requestAnimationFrame`, no scroll listener, no read of `scrollY`. The compositor resolves the transform against the scroll position itself. |
| **Needs a rAF loop?** | **No.** The sharpest difference from WebGL, and what makes option A a different *kind* of thing rather than a cheaper version of the same thing. |
| **Unsupported browsers** | `animation-timeline` (Chrome/Edge 115+, Safari 26+; **Firefox: not assumed**) and `preserve-3d` (universal for a decade). Both degrade **independently and safely**: no timeline → the wrapper sits at its authored rotation; no 3D context → flat faces. With §3's `no-preference` gate, the resting state is the assembled cube either way. |
| **Crawler** | Sees six images or divs in normal document order. No `aria-hidden`, no canvas, no generated text. |

**The real cost centre is not bytes — it is render surfaces.** This is the part a
bundle-size comparison misses entirely:

- Each child of a `preserve-3d` subtree is promoted to its own composited layer
  and needs its own backing texture.
- Six faces at a 640×640 effective size in RGBA: `6 × 640 × 640 × 4 B` =
  **9.8 MB**. At 800×800: `6 × 800 × 800 × 4 B` = **15.7 MB**.
- Add depth and stencil buffers, mip levels for the textures, and the browser's
  own overdraw for the wrapper — call it **20–25 MB of GPU memory** for one
  decorative object.
- On a low-end Android (2–3 GB RAM, Mali-T720/T860 or Adreno 506/610 class), the
  GPU budget is a few hundred MB shared with the compositor and the app itself. A
  20–25 MB 3D subtree is enough to cause texture eviction and jank on scroll —
  **and scroll is exactly the interaction that triggers it.**
- `preserve-3d` subtrees are **painted, not merely transformed**, and during a
  scrub the subtree is re-rasterised as the 3D context resolves.
- §3 already caps scroll-driven work at **six elements per page**. A six-face cube
  consumes the entire page's narrative budget in one object.

**Mobile fallback.** Unacceptable as a scrub. Touch scrolling means the subtree
re-rasterises every frame, on the weakest GPUs, while the compositor is also
handling URL-bar collapse. The honest options are: disable below `lg`, or freeze
at the assembled state. Either way the desktop effect is not the mobile
experience, so it is not "responsive" — it is a desktop-only effect with a static
mobile fallback.

**LCP.** **It cannot be the LCP element.** A `preserve-3d` subtree is a render
surface: no paint can be recorded for it until the 3D context resolves. If the cube
were the largest contentful element, its LCP timestamp would move to whenever the
context resolves. §3's rule — nothing above the fold animates before LCP —
therefore **forces the cube below the fold**, which on the home page means it
cannot be in the hero at all.

**Verdict on A: technically fits at 0 KB, and I still do not recommend it** —
because the costs are GPU memory and scroll jank on the cheapest devices, neither
of which any byte budget here measures, and because §3's six-element cap is
consumed by a single object.

### 5.2 Option B — hand-written WebGL, no library

| | |
|---|---|
| **Dependency cost** | **0 KB.** Raw WebGL2. |
| **JS per frame** | **A `requestAnimationFrame` loop, continuously, for as long as the effect is on screen.** Not optional — WebGL has no declarative timeline. Every frame: set viewport, update matrices, upload uniforms, issue draw calls, resolve the render surface. |
| **Complexity** | You write the scene graph, camera, projection matrix, depth buffer, resize handling, context-loss handling, texture upload, mip generation and shader source. `WebGLRenderer` alone occupies 47 reference sites inside Google's bundle. |
| **Context loss** | WebGL contexts are lost on GPU reset, driver update, backgrounding and memory pressure. Without `webglcontextlost`/`webglcontextrestored` handling, the effect silently dies on exactly the low-end devices you were trying to protect. |
| **Unsupported browsers** | **Nothing renders.** A hard WebGL2 dependency with no fallback beyond a static image. |

**Verdict on B: the worst option.** The 0 KB dependency is a decoy — the real cost
is hundreds of lines of hand-written graphics code, a permanent rAF loop and
context-loss handling, for strictly less capability than option C. Rule it out on
engineering grounds before any byte counting.

### 5.3 Option C — three.js

| | |
|---|---|
| **Dependency cost** | **150–250 KB gz** (§4.1; anchored by a measured **213.4 KB** from Google's Closure-compiled production bundle containing a *full* three.js scene). |
| **Against the 120 KB budget** | **1.25×–2.1× the entire budget, for the animation alone.** Even a hypothetical tree-shaken minimal scene at the bottom of that range leaves nothing for React, Next, TanStack Query, the app's own code, or the ~0.4 KB of View Transitions §3 already budgets. |
| **Route scoping** | §11's rule is that a new dependency must be route-scoped. That **works technically** — `next/dynamic` puts it in an async chunk loaded only when the cube mounts. It does not help the budget: a customer who scrolls to the cube then downloads 150–250 KB *while scrolling*, which is the worst possible moment — the main thread is parsing and executing during the exact interaction the effect is supposed to feel responsive to. |
| **JS per frame** | Same as option B: continuous rAF, plus three.js's own per-frame matrix and uniform churn. |
| **Mobile** | Clamp `devicePixelRatio` or accept the fill cost of §3.3 (~11.3 MB of buffer per frame at DSR 3). Needs an explicit quality tier for low-end devices — more engineering than the effect is worth here. |

**Verdict on C: does not fit, and not by a small margin.** The measured comparator
is decisive: Google's own page spends **504.6 KB gz of JavaScript in total** and
preloads the 213.4 KB three.js bundle on its landing page. We are being asked to
fit 120 KB.

### 5.4 Side by side

| | A: CSS 3D | B: hand WebGL | C: three.js | **D: flat staggered reveal (recommended)** |
|---|---|---|---|---|
| Dependency | 0 KB | 0 KB | **150–250 KB gz** | **0 KB** |
| Share of 120 KB budget | 0% | 0% | **125–210%** | **0%** |
| rAF loop | **none** | continuous | continuous | **none** |
| GPU memory | 20–25 MB (6 layers) | full-viewport target | full-viewport target | **0 extra layers** |
| Can be the LCP element | **no** | no | no | no |
| Reduced-motion end state | static assembled cube | static image | static image | **all faces present** |
| Mobile | desktop-only + static fallback | poor | poor + quality tiering | **identical to desktop** |
| Scroll-jank risk, low-end | **real** | high | high | **none** |
| §3 six-element budget | **all six consumed** | all six | all six | **two to three** |

### 5.5 Verdict

> **Do not build the cube.** Not because it is impossible — option A is real and
> would cost 0 KB — but because on this stack it fails three of our own
> constraints simultaneously: it **cannot be the LCP element** (§5.1), it
> therefore **cannot be the home page's above-the-fold accent**, and its cost
> centre is **GPU memory and scroll jank on low-end phones**, which no byte budget
> in this project measures. Option C fails the budget by 1.25×–2.1× on measured
> evidence. Option B is worse than C at more engineering cost.
>
> If the stakeholder wants the *feeling*, option D delivers it for free.

### 5.6 The cheapest variant that keeps the narrative intent — option D

The cube's narrative value is not the geometry. It is: **parts arrive in
sequence, forming a whole the customer then reads.** That is a sequencing
statement, and §3 already has the mechanism for it.

- **Trigger:** `animation-timeline: view()`, `animation-range: entry 0% entry 40%`.
- **Technique:** the same six elements, laid out as a **flat two-row grid** rather
  than six cube faces. Each face's image gets its own `view()` reveal at a
  progressively narrower `animation-range` (0/8/16/24/32/40%), animating
  `opacity 0→1` and `translateY(12px→0)`.
- **Axes / amplitude:** `translateY` capped at **12px**, inside §3's existing 6% /
  24px ceiling. No rotation.
- **Layer budget:** **one transient promoted layer per face**, all of which §3
  already budgets at ≤ 6 per page. No `preserve-3d`, so **no persistent 3D render
  surface and no 20–25 MB subtree.** On mobile the identical effect runs.
- **Reduced motion:** every face is **present at opacity 1** — the whole grid,
  static. Because the animation lives inside
  `@media (prefers-reduced-motion: no-preference)`, that is the default state, not
  a fallback branch.
- **Crawler / LCP:** six images in document order, no canvas, no generated text.
  The LCP element stays the hero, untouched.
- **Per-route JS cost:** **0 KB.** Identical to today.
- **What is given up:** depth. The faces have no sides; there is no parallax
  between them as the page turns. **This is a downgrade and it is labelled as
  one.** It preserves "assembles in order as you read"; it does not preserve
  "assembles as a solid".

**One honest zero-cost upgrade** if a little depth is wanted: give each face its
own `rotateY(±4deg)` and `translateZ(±8px)` within the **same flat grid**, with
**no `preserve-3d` on the shared parent** — so each face gets its own small
render surface rather than one 3D context spanning six children. That is a genuine
parallax hint for a few bytes of CSS, keeps the layer count at six transient
surfaces rather than one persistent 3D subtree, and is discarded entirely under
reduced motion. **It is not a cube.**

### 5.7 If it were built: spec entry, recorded so the option stays priced

| Field | Value |
|---|---|
| Trigger | `animation-timeline: view()` on a `140vh` wrapper, `animation-range: entry 0% cover 60%` |
| Technique | `perspective: 1200px` on the container; `transform-style: preserve-3d` on the wrapper; six faces at `rotateY(n·90deg) translateZ(w/2)`; wrapper `rotateY(-25deg→0deg)` on the scroll timeline |
| Axis cap | **One axis only.** `rotateY` on the wrapper, `rotateX` ≤ 6° on faces. No combined `rotate3d`, no Z translation of the wrapper |
| Amplitude cap | Wrapper rotation **≤ 30° total**; per-face `translateZ` ≤ 25% of face width; opacity 0→1 across the first 40% |
| Layer budget | **6 faces + 1 wrapper = 7 promoted layers, hard cap.** Exceeding it means the effect is cut, not the budget raised |
| Texture budget | **≤ 6 × 100 KB.** Prefer gradients or SVG over photographic faces. §10's AVIF/WebP `next/image` pipeline applies, and the cube is never the LCP image so it never gets `priority` |
| Placement | **Below the fold, `lg` and up only.** Desktop-only; phone renders the flat option D grid |
| Reduced motion | `preserve-3d` removed, wrapper at `rotateY(0)`, all faces `opacity: 1`. The `preserve-3d` declaration itself must be gated, not just the animation |
| LCP | Cannot be the LCP element. Any `priority` image stays outside the subtree |
| CLS | **Zero by construction** — fixed aspect box on the wrapper; `contain-intrinsic-size` if `content-visibility` is on the parent |
| Per-route JS | **0 KB.** The only reason option A is on the table at all |

---

## 6. Is the cube a better use of the home page's accent moment?

§3.1 and the `STITCH-PROMPTS.md` 3.1 block both give the home page exactly one
accent moment: **the three page sections settling in as they are reached** — a
`view()` reveal per section, two or three of them, nothing else moving.

The cube would be a **fourth, very large, scroll-scrubbed** moment. It conflicts
with the existing one on three counts:

1. **P2 — one accent moment per page.** A cube plus three reveals is two accents.
2. **§3's six-element budget.** Six faces would consume it entirely; three reveals
   would be gone.
3. **LCP.** The cube cannot be the LCP element, so it must sit below the hero —
   meaning the home page's *first* impression, which is what the existing reveal
   paces, is the part the cube could never touch.

**This is a decision for the user, not for me.** Three options, stated plainly:

| Option | Home-page accent | Cost | Keeps P2? |
|---|---|---|---|
| **(a) Keep the current reveal** | Three sections settle on scroll | 0 KB | yes |
| **(b) Replace with option D** | Six product images in a flat grid assemble in sequence as they scroll into view | 0 KB | yes — one moment, but a stronger one, and it puts the movement on the merchandise instead of the section furniture |
| **(c) Build the real cube** | Six faces assemble into a cube on scroll | 0 KB, 20–25 MB GPU, desktop-only | **no** |

**My recommendation is (b) over (a)** — it is free, it keeps P2 intact, it puts the
single accent moment on the products rather than on a section heading, and it is
the closest honest relative of what the stakeholder asked for. **(c) I recommend
against**, for the reasons in §5.5.

**Until the user chooses, `UI-UX-SPEC.md` §3.1 and the `STITCH-PROMPTS.md` 3.1
block remain unchanged and (a) is the contract.** Nothing has been quietly
rewritten.

---

## 7. Audit of the spec's own reasoning

### 7.1 The CSP argument against animation libraries is **wrong**. The stakeholder is right.

**The claim under audit**, `UI-UX-SPEC.md` §10.5, verbatim:

> - **No third-party script** — which is precisely why §11's total is 0 KB, and
>   precisely why the Razorpay problem (§12-G8) is a real blocker rather than a
>   "just add the domain" chore.

**The claim is invalid, and the inference "which is precisely why §11's total is
0 KB" is the invalid part.**

`frontend/next.config.ts:16` sets:

```
const scriptSrc = ["'self'", "'unsafe-inline'"];
```

`'self'` in `script-src` means **the document's own origin**. An npm dependency
installed into this app is bundled by Next into `/_next/static/chunks/*.js` and
served from the app's own origin — same-origin, therefore satisfying `'self'`,
therefore requiring **no CSP change of any kind**. GSAP, motion, three.js or any
other bundled package are all permitted by the CSP exactly as they are permitted
today.

What the CSP *does* block is a **script fetched from a third-party origin** — a CDN
URL, a tag-manager snippet, an externally hosted SDK. That is precisely and only
what §12-G8 is about: `src/lib/razorpay.ts:19` injects
`https://checkout.razorpay.com/v1/checkout.js`, which `'self'` does not cover.

**Corrected reasoning, which is now what the spec says:**

- The CSP constrains **where third-party code may be fetched from**. It does not
  constrain **how much first-party bundled code may ship**.
- Therefore the CSP places **no constraint** on GSAP, motion, lenis, three.js or
  any other npm dependency.
- The **only real constraint on animation dependencies is the 120 KB per-route
  first-load JS budget** (§10.2), plus the per-frame and layer-cost analysis in
  §3 of this file.

**What was defensible and stays.** The CSP genuinely does load-bearing work in
two places, and neither is weakened by this correction:
- §12-G8 remains a **real P0**: the payment step genuinely cannot load the gateway
  today, and fixing it genuinely requires widening `script-src` plus adding
  `frame-src`/`child-src`. Unchanged.
- §10.5's closing line — that the §3 motion system is pure CSS and therefore needs
  no new `style-src` allowance — is still correct, and now for a better reason:
  not "CSS is free of the budget", but "CSS needs no allowance in a CSP that
  already permits `'unsafe-inline'` for styles".

**A note on why the error happened**, because it is worth recording: the
first-round analysis reasoned from "the CSP forbids third-party scripts" to "we
therefore ship no dependencies". Those are different claims, and only the second
one needed evidence. The evidence for the second one exists independently — §11.2
enumerates ten requirements and shows the platform satisfies all ten — so the
conclusion was right and one of its stated reasons was wrong.

### 7.2 Does the CSP affect anything in this analysis?

| Question | CSP effect |
|---|---|
| CSS 3D cube (§5.1) | **None.** No script involved |
| Hand-written WebGL (§5.2) | **None** — but it would need `worker-src` if offloaded to a Worker; same-origin, so permitted. The obstacle is engineering, not CSP |
| three.js (§5.3) | **None.** Bundle it and it satisfies `'self'`. The obstacle is the 120 KB budget, not the CSP |
| Lenis / GSAP / motion | **None.** All bundleable |

**The CSP constrains none of these choices.** Every rejection in §5 rests on
runtime behaviour, GPU memory, LCP eligibility or measured bytes.

### 7.3 Other spec rows this round's evidence touches

| Row | Finding | Action |
|---|---|---|
| §3 R3 (GSAP) | "~31 KB gz combined (see §11) — 26% of the entire 120 KB budget" | **Corrected** to **46.3 KB gz / 38.6%**. The rejection strengthens |
| §11.3 (GSAP) | same figures | **Corrected** in place |
| §11.3 (motion) | "~18–34 KB full; ~2.6–5.5 KB mini" | **Corrected**: full build **measured 46.7 KB gz**; the `mini` figure **could not be verified** and is now marked as an unverified estimate |
| §11.3 (Lenis) | "~3.5 KB gz" | **Corrected** to **5.4 KB gz** (measured). Rejection unaffected — it rests on R4's behavioural grounds |
| §12-G8 | Razorpay blocked by CSP | **Confirmed still true, still P0, still record-only.** The correction in §7.1 narrows the CSP's role to this row and to nothing else |
| §13 A-6 | package sizes were estimates | **Replaced** with measured values; a residual note added for `motion/mini` |
| §13 A-17 | no Lighthouse/axe/bundle run | **Replaced** with what is now known: external sites measured via HTTP transfer size; **our own app still has no build, no bundle and no Lighthouse run** — that part is unchanged and remains open |

### 7.4 What I still cannot verify

- **No build of our app.** I have no `node_modules` and did not run `next build`.
  The claim that a bundled dependency is served same-origin from
  `/_next/static/` is from Next's documented behaviour and the config, **not from
  observing our build output.** A `next build && ls _next/static/chunks` would
  confirm the exact chunk filenames.
- **No Lighthouse on our app.** Every §10 target remains a target.
- **CodeWiki's render behaviour.** Renderer identified; effect not seen (§3.1).
- **`motion/mini` shipped size.** Unresolved (404 on every path tried).
- **Third-party site main-thread cost, request priority and lazy-load behaviour.**
  Not observable without a rendering browser. All site figures are transfer-size
  only.

---

## 8. Assumptions added or revised this round

| # | Assumption | Status |
|---|---|---|
| **A-6 (revised)** | Package sizes | **Replaced by measurement.** three 419.7 KB gz unminified (ceiling) / ~213.4 KB gz in Google's Closure build (practical anchor); gsap 28.3 KB gz; ScrollTrigger 18.0 KB gz; motion 46.7 KB gz; lenis 5.4 KB gz. Residual: `motion/mini` unverified |
| **A-17 (revised)** | No performance measurement existed | **Replaced with what is now known.** Seven third-party sites measured by HTTP transfer size (M) with the category limits in §0.1. **Our own app remains unmeasured** — no build, no bundle, no Lighthouse |
| **A-18** *(new)* | `curlimage`/desktop-Chrome UA is representative of a first-time visitor's request set | Reasonable — Shopify serves the same document to bots and users — but a returning visitor with warm HTTP cache would transfer far less. Figures are **cold-cache first load** |
| **A-19** *(new)* | Effective cube face size of 640–800 px | Used only for the §5.1 GPU-memory arithmetic. At 400 px the figure halves to ~6 MB and the low-end-device argument weakens; at 1200 px it doubles to ~47 MB and strengthens. **The conclusion is robust across 400–800 px and fragile above 1000 px** |
| **A-20** *(new)* | `devicePixelRatio` 3 at 390×844 for the §3.3 fill calculation | Typical for a current mid-range Android; low-end devices are often 2.0–2.5, which *reduces* the WebGL fill cost but also correlates with weaker GPUs. The direction of the argument is unchanged |
| **A-21** *(new)* | The three.js figure will not shrink below ~150 KB gz in a production build | Bounded by the measured 213.4 KB from a Closure-optimised build of the *full* library. If a minimal scene tree-shook to 60 KB, option C would become arguable — I saw no evidence for that and it would be a large departure from the measured anchor |
| **A-22** *(new)* | The CodeWiki cube is on a route I did not retrieve | §3.1. The technology finding holds regardless; the load-behaviour detail in §3.4 may not |

### 8.1 Round 3 assumptions — the throughput model

Added 2026-10-03. **These concern §11–§13 only.** A-18–A-21 are unchanged.

| # | Assumption | Status |
|---|---|---|
| **A-23** *(new)* | **The 200 KB/s and 50 KB/s figures are MODEL CONSTANTS**, not measurements. I imposed them with curl's `--limit-rate` and measured real transfers under them, but the *rates* are conventional mobile-proxy values, not a phone on a network | **The single caveat that decides §11.** If real users are on 1.6 Mbps rather than 400 Kbps, the penalty in §11.3 is +0.60 s rather than +2.40 s. Both are derived from the same measured 280 KB slope, so the *ratio* (218×) is more trustworthy than either absolute |
| **A-24** *(new)* | **`--limit-rate` did not engage below ~100 KB** on curl 8.21/Windows — sub-100 KB payloads returned unthrottled totals | **Disclosed in §11.1.** Consequence: candidate-by-candidate throttled timings are **modelled from the validated 280 KB slope**, not measured. No conclusion depends on them |
| **A-25** *(new)* | **Connection setup ≈ 0.14 s** is measured **to a nearby CDN edge from a wired/wifi host**. Real mobile adds 50–150 ms RTT and the handshake stacks, so per-connection cost is more likely **0.3–0.6 s** on a phone | Understates the mobile penalty. The bias is in the conservative direction for our argument |
| **A-26** *(new)* | **JS parse+compile ≈ 1 ms per 100–200 KB on a mid-range phone** (used for the +0.6–1.2 s hydration figure in §11.4) | **Model constant, widely used, not measured here.** It is the weakest number in this document. §11.4's conclusion does not depend on it — the +2.4 s bandwidth figure already carries the argument on its own |
| **A-27** *(new)* | **LCP contention is "up to ~1.2 s"** — i.e. roughly half the added bandwidth occupancy, assuming serialisation rather than perfect interleaving | **Model.** Our LCP element is an image, so this is contention arithmetic, not a measured regression. Untested on a real page |
| **A-28** *(new)* | **The ~250-line / 5-file / 6-task pivot estimate in §10.2** | **Order-of-magnitude (E), not measured** — there is no build and no builder ran. Derived from §3's motion table (only ~6 rows are scroll-driven) and §6's inventory. The *shape* is defensible from the architecture; the exact number is not |
| **A-29** *(new)* | **`motion`'s `mini` entry cannot deliver scroll-linked work** | **E, from documentation not retrieved.** `mini` is described as animate-only. If it *did* include scroll timelines, tier (b) in §10.1 would cost far less than the measured 46.7 KB full build. **This is the assumption most likely to be wrong and worth checking**, because it is the only place where a cheaper option might exist |
| **A-30** *(new)* | **The deferred-chunk arrival times in §12.4** (0.09 s measured unthrottled; ~0.95 s modelled at condition C) | Mixed, as labelled. Note the 0.09 s figure is a real measurement from §11.1's small-payload runs; the 0.95 s is modelled from the A-23 constant |

**None of these is a blocker.** The recommendation in §5.5 and the correction in
§7.1 both survive every assumption above; §5.5 is a "no" that a more optimistic
GPU assumption would not turn into a "yes", because the LCP-eligibility and
six-element-budget arguments in §5.1 are independent of hardware.

> **Round 3 added assumptions — see §8.1 below.** It does not revise A-18–A-21;
> it adds to them.

---

## 9. Provenance

| Source | Retrieved | Method |
|---|---|---|
| `codewiki.google` shell + 2 JS bundles + CSS | 2026-10-03 | `webfetch` html; `curl.exe` for each asset; gz computed locally |
| `npm view three/gsap/motion/lenis` | 2026-10-03 | registry metadata |
| unpkg package builds (three, gsap, motion, lenis) | 2026-10-03 | `curl.exe` download, `.NET GZipStream(Optimal)` |
| 7 reference/inspiration sites | 2026-10-03 | `curl.exe -L` with desktop Chrome UA; `Accept-Encoding: gzip` |
| `frontend/next.config.ts`, `frontend/package.json` | this worktree | read directly |

No application code was written, no dependency installed, `package.json`
untouched, no `refs/` directory, no committed images, no MCP, no Stitch API.

---

# Round 3 — the "client wants more motion" question

> Added 2026-10-03. The question: **we build the current CSS-first spec; the
> client then says he wants animations even at the cost of optimisation.** What
> does that cost, what does doubling the budget buy, are theatre and speed
> actually in conflict, and how much capability does a budget actually ration?
>
> **Nothing in `UI-UX-SPEC.md` is changed by this section.** Every option is
> presented for the user's decision. §11.4 of that file carries the quantitative
> summary; this file carries the evidence.

---

## 10. ONE — the rewrite cost of being wrong

### 10.1 The three tiers, costed

| | **(a) current CSS-only** | **(b) plus `motion`** | **(c) plus GSAP + ScrollTrigger** |
|---|---|---|---|
| Added to first-load budget | **0 KB** | **+46.7 KB** (measured, full build) | **+46.3 KB** (measured) |
| Share of the 120 KB budget | 0% | 38.9% | 38.6% |
| Fits a doubled 240 KB budget | — | yes, 19% | yes, 19% |
| Techniques gained | the 7 classes in §3 | **+scrub, +physics** | **+scrub, +physics** |
| Accent moments gained | — | **0** (P2 forbids it) | **0** (P2 forbids it) |

**A finding worth stating before the rest: tiers (b) and (c) are substitutes, not
complements, and they cost the same.** `motion`'s `mini` entry is animate-only —
no scroll timelines — so scroll-scrub work needs the **full** build, which I
measured at **46.7 KB gz**, not the "~2.6–5.5 KB" that §11.3 originally carried as
an estimate. So "add `motion` mini cheaply" is not on the table for the use case
the stakeholder actually wants. Both real options land at ~46 KB.

### 10.2 The load-bearing finding: the token layer makes a late pivot cheap

This is the single most decision-relevant number in this round.

**§2.4 puts every duration and easing behind a token** (`--motion-enter-dur`,
`--motion-narrative-dur`, …, eight of them, all in `@theme` in `globals.css`).
**§3 enforces reduced motion at the token layer**, not per component. **P2 fixes
one accent moment per page.** Together these mean a pivot touches *durations and
eases*, not *components*.

| | Files touched by the pivot |
|---|---|
| **With the token layer (as specced)** | `globals.css` (token block) + a new TS token mirror + **3 scroll-driven surfaces only** — home reveals, PDP story parallax, tracking markers = **~5 files** |
| **Counterfactual: per-component motion** | every component that moves. §6's inventory is 30 existing + 6 new. Motion appears on roughly 20 of them = **~31 files**, and 20 independent chances to forget the `no-preference` gate |

**≈ 6× fewer files.** But the file count is the *lesser* benefit. The larger one:

> Under per-component motion, the reduced-motion guarantee is a **convention**
> someone has to remember 20 times. Under the token layer it is a **language
> feature** the platform enforces — and it can be pinned by a single test
> (`motion-tokens.test.ts`, §9.1) and three greps (§7.8).

A late pivot under the token layer is cheap **in both senses**: fewer files to edit,
and one guarantee instead of twenty.

**Code delta, honestly (E — order-of-magnitude, not measured, because there is no
build and no builder ran):**

| Piece | Estimate |
|---|---|
| TS token mirror of the eight CSS motion tokens | ~40 lines |
| Reduced-motion JS mirror (`matchMedia` → a single boolean the timelines read) | ~20 lines |
| GSAP/motion bootstrap, route-scoped, `next/dynamic` | ~60 lines |
| Home reveals, converted to one scrubbed timeline | ~50 lines |
| PDP story parallax | ~40 lines |
| Tracking markers | ~40 lines |
| **Total** | **~250 lines**, in 5 files |

The **majority of §3's motion table does not move.** Every `state`, `enter`, `exit`,
`hover`, `toast`, `progress` and `loading` row stays CSS and *should stay CSS* —
porting a 120ms opacity transition to GSAP would be a regression, not an upgrade.
Only the six or so rows marked `animation-timeline: view()/scroll()` need JS at all.

**In builder-sized tasks, using this repo's own convention (SPEC-x-Bnn cycles):**
**6 tasks** — (1) token mirror + bootstrap, (2) home reveals, (3) PDP parallax,
(4) tracking markers, (5) reduced-motion parity + a11y verification, (6) one audit
cycle. Not a rewrite. **Not a re-audit of the whole spec** — the audit surface is
five files.

### 10.3 What is genuinely **not** cheap to pivot — the honest half

The token layer does not help when motion is entangled with something structural.
Four cases, in descending order of pain:

| Entanglement | Why the token layer cannot save it | Cost |
|---|---|---|
| **The LCP element** | §3 forbids animating above the fold. A client request for "the hero assembles" is not a code delta — it is a **decision reversal** that accepts an LCP regression on `/` and `/products/[slug]`. No budget fixes this. | **Architectural.** Needs the user to change §10.1, and to re-own the LCP target. |
| **The reduced-motion grep gate** | §7.8 check 2 greps for `duration-[` and scroll timelines **in CSS**. GSAP-driven motion is invisible to those greps, so the audit surface itself has to be **rewritten** before the auditor's checks mean anything again. | **Audit-surface change**, not a code change. Cheap to write, expensive to get wrong. |
| **Layout-entangled motion** | §3 deliberately routed cart-line removal through View Transitions instead of a height animation (R1). A request for "the panel slides open" means a real layout animation, which touches `CartView`, `CheckoutView`, `MenuOverlay` **and their focus management**, and re-opens R13's hazard: a moving target under the cursor. | **3–5 components plus full a11y re-verification.** The genuinely expensive one. |
| **Spring/preset physics** | GSAP's defaults have no defined end state, which is exactly why R14 rejects springs: it makes the reduced-motion override impossible to express as a token. Adopting GSAP means **re-imposing the token discipline by hand** — a convention to maintain, not a feature to inherit. | **Ongoing, unbounded.** The cheapest to add and the most expensive to keep. |

**Net position: a late pivot to *more expressive scrub* is cheap — about 250 lines
across 5 files, 6 tasks. A late pivot to *motion entangled with layout, the hero, or
the audit surface* is expensive, and no amount of budget changes that.**

---

## 11. TWO — what doubling the budget buys, in time

### 11.1 Measured throughput on this machine

`curl.exe` against unpkg with `Accept-Encoding: gzip`, so `size_download` is the
gzipped payload on the wire. `--limit-rate` used to impose a rate cap.
**M = measured.** Runs repeated where noted.

| Condition | Payload | Size (gz) | TTFB | Total | Repeat variance |
|---|---|---|---|---|---|
| **Unthrottled** | `three.core.js` | 286,751 B | 0.140–0.162 s | **0.166 / 0.175 / 0.193 s** (3 runs) | ±8% |
| **@ 200 KB/s** | `three.core.js` | 286,751 B | 0.147–0.157 s | **1.164 / 1.166 s** (2 runs) | **±0.2%** |
| **@ 50 KB/s** | `three.core.js` | 286,751 B | 0.152–0.176 s | **5.157 / 5.177 s** (2 runs) | **±0.4%** |

Derived from the unthrottled run: fixed connection cost (DNS + TCP + TLS + TTFB)
≈ **0.14 s**, leaving a transfer phase of **0.026–0.030 s** for 280 KB —
a marginal rate of **≈ 9.6–10.8 MB/s**. **On this machine 280 KB is latency-bound,
not bandwidth-bound.** That is the single most important fact about this
measurement and it is the reason the numbers below spread so widely.

**Two measurement caveats, stated because they changed how I ran it:**

1. **`--limit-rate` did not engage on the small payloads.** Attempts to throttle
   `gsap.min.js` (28 KB), `ScrollTrigger.min.js` (18 KB) and `motion.js` (47 KB)
   returned unthrottled totals. Only the 280 KB payload was large enough for the
   limiter to bite. **So the throttled figures below are modelled from the
   validated 280 KB slope, not measured per-candidate.**
2. **curl's limiter overshoots its nominal cap**: measured ≈ 277 KB/s against a
   200 KB/s cap (1.39×), and ≈ 55.8 KB/s against a 50 KB/s cap (1.12×). The models
   below use the **nominal** rates, which are therefore slightly conservative.

### 11.2 Two network conditions — and which is which

| Condition | Downlink | Status | Latency assumption |
|---|---|---|---|
| **A — this machine** | **≈ 9.6–10.8 MB/s marginal (M)**, ~0.14 s fixed cost (M) | **MEASURED** | as measured (M) |
| **B — 1.6 Mbps mobile** | 200 KB/s | **MODEL CONSTANT** (a widely used DevTools-proxy figure) | ~0.15 s (M, this machine, to a nearby edge) |
| **C — 400 Kbps mobile** | 50 KB/s | **MODEL CONSTANT** | ~0.15 s (M) |

Conditions B and C are **modelled rate constants applied to measured latency and
measured gzip sizes.** They are not measurements of a phone on a network. Real
mobile RTT is worse than 0.15 s — 50–150 ms is typical, which on top of a TLS
handshake can put connection setup at **0.3–0.6 s**, and that penalty applies to
*every* additional connection, not just the first-load bundle.

### 11.3 120 KB → 240 KB gz: the arithmetic

Transfer time for the JS payload, plus one measured ~0.15 s connection cost:

| | 120 KB | 240 KB | **Delta** |
|---|---|---|---|
| **A — this machine** (M slope) | 0.15 + **0.013 s** | 0.15 + **0.025 s** | **≈ +12 ms** |
| **B — 1.6 Mbps** (model) | 0.15 + **0.60 s** | 0.15 + **1.20 s** | **≈ +0.60 s** |
| **C — 400 Kbps** (model) | 0.15 + **2.40 s** | 0.15 + **4.80 s** | **≈ +2.40 s** |

> **A 218× spread between the machine I measured on and a throttled phone.**
> The same decision is free here and is 2.4 seconds there.

**I am not averaging these.** The whole point of the table is that the two
conditions produce opposite answers, and the customer is on condition C.

### 11.4 What doubling the budget does to each metric

| Metric | Effect of +120 KB on the critical path | Confidence |
|---|---|---|
| **LCP** | **Indirectly delayed, up to ~1.2 s on condition C.** Our LCP element is an image (§3), not JS, so the effect is *contention*, not sequencing: the extra 2.4 s of bandwidth occupancy competes with the LCP image's own download. If serialised, ~half that lands directly on LCP. **Not measured — modelled from bandwidth occupancy.** | **Model** |
| **Time-to-interactive** | Deprecated as a Core Web Vital in favour of INP, but it is still the honest name for "when can you click". Parse+compile is roughly **1 ms per 100–200 KB on a mid-range phone**, so +120 KB ≈ **+0.6–1.2 s of main-thread time before hydration**, serialised against React hydration and the auth/cart boot. **Model constant, not measured.** | **Model** |
| **INP** | **No improvement, and a real risk of regression.** INP is dominated by *long tasks during interaction*, not by bundle size. A bigger bundle means more main-thread work competing with scroll and tap handlers. | **Model** |
| **Visitor on a mid-range phone** | **+2.4 s before the page is interactive, on a connection where they already wait ~2.5 s for what we have.** Roughly a **doubling of time-to-interactive** for a change that is decorative. | **Model**, from §11.3 |

### 11.5 What doubling the budget does **not** buy

This is the part that survives every argument about how much time 120 KB is worth:

- **INP.** Not a byte metric. Adding bytes to a JS bundle does not improve it and
  can harm it.
- **Main-thread time.** Parse, compile, evaluate and the per-frame cost of any
  animation library are separate axes. More bytes means *more* of both.
- **Composited layer count.** Governed by `will-change`, `transform-style`,
  `contain` and how many elements move — not by bundle size. A GSAP timeline can
  promote *more* layers than CSS, because GSAP writes inline styles that the
  compositor then has to work out.
- **The number of accent moments.** P2 forbids a second one **regardless of
  budget** (§13.4).
- **The six-simultaneous-element cap.** §3's cap is a *cognitive* limit. Bytes
  never had a vote.
- **The LCP element's eligibility to animate.** Physics, not budget (§10.3).

**So the doubling spends 2.4 s on condition C to buy one technique class (§13).**

---

## 12. THREE — the middle point: are theatre and speed actually in conflict?

**Mostly no.** The tension is real but it is **sequenced badly, not priced badly**.

### 12.1 The hypothesis, tested

> The binding constraint is **sequencing**, not bytes. A moment that begins after
> LCP, is route-scoped, and is dynamically imported can be arbitrarily richer at
> **zero cost to first load**. The same moment on the critical path cannot.

This holds, and it holds *because of a rule already in the spec*:

> **§3: "The default state is the final state."** Every scroll-driven animation
> lives inside `@media (prefers-reduced-motion: no-preference)`, so an element's
> resting style **is** its completed style.

That single rule is what makes deferral free. **Because the enhanced version and
the un-enhanced version are identical at rest, deferral needs no loading state, no
skeleton, no progressive-enhancement branch and no layout shift.** The static page
is already the finished page.

### 12.2 The concrete architecture

| Stage | What happens | Cost to first load |
|---|---|---|
| **HTML arrives** | Server component. Every accent element is present, in its **final position and final opacity**. Correct, complete, indexable. | 0 KB |
| **LCP paints** | Hero/PDP image. **No motion has run and none has been waited for.** | 0 KB |
| **Section approaches the viewport** | The section is already visible and static. A `next/dynamic` import is issued for the richer timeline chunk — **on a route the customer has already chosen**. | 0 KB to first load |
| **Chunk resolves** | The scrub timeline attaches and takes over from the static state. | **46.3 KB, off the critical path** |
| **Reduced motion** | The chunk is **never requested at all** — the `no-preference` gate is CSS, and the dynamic import is gated on the same boolean. | 0 KB |

### 12.3 "Theatre that silently never plays" — the failure mode, and its fix

On a slow connection the chunk may take 0.95 s (condition C, §11.3). What does the
customer see in that window?

**The finished, static, assembled state.** Which is a *designed* state, not an
absence — and this is the whole answer. §3 already mandates it. So the failure mode
is not "theatre fails", it is **"theatre is replaced by the thing theatre was
decorating"**, which is the correct degradation and costs the customer nothing
they came for.

Three design rules that make this real rather than theoretical:

1. **No spinner, ever, for a deferred accent.** A spinner implies something is
   missing. Nothing is missing.
2. **No fade-in.** If the section is already at final opacity and a library fades
   it from 0, the library has *introduced* the failure mode. Deferred motion must
   attach to an already-correct element.
3. **No layout shift on arrival.** The element must not change size when the chunk
   lands. If the richer version is the same six faces in the same grid
   (`MOTION-3D-ANALYSIS.md` §5.6 option D), attaching a timeline changes nothing
   measurable.

### 12.4 What the sequencing architecture buys, in KB

| | On the critical path | Deferred (after LCP, route-scoped) |
|---|---|---|
| The 46.3 KB GSAP/ScrollTrigger chunk | **+46.3 KB first load** → **+2.4 s** on condition C (§11.3) | **+0 KB first load**; 46.3 KB arriving during scroll, ≈ 0.09 s (M, this machine), ≈ 0.95 s (model, C) |
| Effect on LCP | indirect contention, up to ~1.2 s (model) | **none** — it is not fetched until after LCP |
| Effect on hydration | +0.6–1.2 s main-thread before interactive (model) | **none** — separate chunk, evaluated on arrival |

> **The same 46 KB is either a 2.4-second penalty or free, depending entirely on
> which line of the timeline it sits on.** That is the decision-relevant framing of
> the stakeholder's question, and it has nothing to do with whether we can afford
> 46 KB.

### 12.5 The honest part: the residual gap at 0 KB

If the client spends nothing, what is genuinely unreachable? Naming it precisely,
so it is not inflated to justify a budget:

| Gap | Real? | Cost to close |
|---|---|---|
| **Continuous scrub** tied to scroll *position* with easing curves, rather than a staggered reveal at the entry threshold | **Yes — the one real gap.** §3's reveals are threshold-triggered, not position-scrubbed | 46.3 KB deferred, ~0 first load |
| **Shared-element continuity across route changes** (a card becoming a detail-page image) | Partly — same-document View Transitions cover the in-page cases | ~0.4 KB for in-page; cross-document is Chrome-only |
| **Gesture / physics** | Yes, but **not wanted on this design.** R14 rejects springs because no defined end state makes the reduced-motion override unexpressible as a token | not recommended at any budget |
| **Theatrical *staggered* assembly** (the cube's actual narrative content) | **No gap.** Reachable at 0 KB via option D (§5.6) | 0 KB |
| **Parallax depth, progress fills, 3D-ish face rotation, hover, state, toast, menu, skeleton** | **No gap.** All CSS | 0 KB |

> **Most of the theatrical intent is already reachable at 0 KB. The residual gap is
> one technique — continuous scroll-position scrub — and it can be bought for 46.3 KB
> that never touches first load.** I am not going to inflate that into a case for a
> larger budget, because it is not one.

---

## 13. FOUR — the marginal value curve, and what is actually rationing the design

### 13.1 KB spent against what it purchases

| Cumulative KB | What you have bought | Technique classes | Accent moments | Simultaneous animated elements |
|---|---|---|---|---|
| **0** | §3 as specced: entrances, hover/state, progress fills, sticky parallax, toast, menu overlay, skeleton, and the two View Transitions | **8** | 6 (one per page) | ≤ 6 per page |
| **+0.4** | cross-element shared continuity (add-to-cart morph, cart-line close-up) — already in §10.2 | **9** | 6 | ≤ 6 |
| **+46.3** | **continuous scroll-position scrub**, with curves, plus physics | **10** | **6** | **≤ 6** |
| **+213** | real 3D geometry, lighting, shadows, tone mapping | **11** | **6** | **≤ 6** |

### 13.2 Where the curve is flat — and where it is cliff-shaped

**Cliff-shaped at the bottom.** The first **0.4 KB** buys an entire technique class
(shared-element continuity) — the highest yield per byte anywhere on this list, and
already committed in §10.2.

**Almost flat above that.** Each further tranche buys **exactly one** technique
class and **zero** accent moments:

- 46 KB buys scrub. It does **not** buy a 7th accent moment — **P2 forbids that at
  any budget.** It does not raise §3's six-element cap. It does not let anything
  above the fold animate. It does not improve INP, and it slightly worsens it.
- 167 KB more buys 3D. Same: one class, still six elements, still barred from the
  hero, and it eats the entire 240 KB budget to do it.

**A 460 KB budget would buy the same six accent moments as a 0 KB budget.** The
curve is not linear, and past ~1 KB it is nearly horizontal.

### 13.3 So what *is* rationing the design? Not bytes.

| Rationing constraint | Kind | Can budget buy it out of? |
|---|---|---|
| **P2 — one accent moment per page** | Cognitive / brand | **No.** A second accent is forbidden regardless of spend |
| **§3's ≤ 6 simultaneous animated elements and 6% / 24px amplitude cap** | Cognitive | **No.** Bytes never had a vote |
| **§3's LCP rule — nothing above the fold animates before LCP** | Physics | **No.** Physics, not currency |
| **§2.4's token layer + §3's `no-preference` gate** | Architectural | It is not a limit — it is what makes the design cheap to change |
| **The 120 KB budget** | Economic | Yes — and it is rationing the **least** valuable thing on this list |

> **The budget is rationing one technique class. The accent-moment count, the
> amplitude, and whether the hero may move are rationed by rules that no amount of
> money overrides.** The client's instinct — that more bytes buy more theatre — is
> the one relationship in this document that does not hold.

### 13.4 "If we double it to 240 KB, here is exactly what we would build"

Concrete, in priority order, with the residual explicitly unspent:

| # | Spend | KB | Route / when | Buys |
|---|---|---|---|---|
| 1 | **§3's existing CSS motion system on all 17 routes** — already specced, already zero | **0** | all | the 8 baseline technique classes |
| 2 | **The two View Transitions** — already specced in §10.2 | **0.4** | PDP, cart | shared-element continuity |
| 3 | **Deferred GSAP + ScrollTrigger for exactly three surfaces**, `next/dynamic`, gated on `prefers-reduced-motion: no-preference`, never on the critical path | **46.3** | home flat-grid assembly; PDP story parallax; tracking markers | continuous scrub, with curves |
| 4 | **Left unspent** | **~73** | — | — |

**And what I would refuse to buy even at 240 KB, with the reason:**

| Not buying | Why |
|---|---|
| **three.js (213 KB)** | It would consume the entire doubled budget to buy one technique class — and that class still cannot appear above the fold. §5.5 |
| **A second accent moment on any page** | P2. Not a price question |
| **Sprite-sheet or canvas 3D** | Same 20–25 MB composited cost as §5.1 with none of the restraint |
| **Raising §3's six-element or amplitude caps** | Cognitive limits, not byte limits (§13.3) |
| **Any motion on `/checkout`'s money states** | R13: the element carrying "Retry payment" must be available the frame the state changes |

### 13.5 My recommendation among the tiers

> **None of them, as things to buy now. The client does not need to spend anything.**
>
> The evidence: 6 of the 8 baseline technique classes cost **0 KB**; the entire
> theatrical *narrative* the cube promised — parts arriving in sequence, forming a
> whole — is reachable at **0 KB** via §5.6 option D; the one genuine gap is
> continuous scrub, and §12 shows it can be bought for 46.3 KB that **never touches
> first load**. The only budget that is demonstrably painful is one raised *without*
> changing the sequencing, and §11.3 prices that at **+2.4 s on a 400 Kbps
> connection** to buy **one** technique class and **zero** accent moments.
>
> **If the client insists on spending, spend row 3 of §13.4 — 46.3 KB, deferred,
> after LCP — and leave the other ~73 KB unspent.** That is the version of "yes"
> that does not cost the customer 2.4 seconds.

---

---

---