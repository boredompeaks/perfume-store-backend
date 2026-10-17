# Wave plan W10–W15 — ALL 16 design-pages implemented live, backend-parity, 1hr-rebrand

Owner: ui-orchestrator. Tree: `spec-ui` @ `94cb723` (all four worktrees
identical, origin verified). Written after SYNC-W10 + CONTRACT-SWEEP-W10.
**Revision 2 (owner directive): all design-pages implemented live** — every
one of the 16 Stitch screens is rebuilt on its route against the actual
`screen.png` + `code.html`, design-override on ambiguity (D1), not a light
polish pass. **Not committed, not dispatched — awaiting owner go.**

---

## 1. Standing decisions (owner, this session)

| # | Decision | Effect on every handoff |
|---|---|---|
| D1 | **On ambiguity the design overrides the spec.** Full-page rebuilds against the screens, "a little tweaking with taste is fine". | Every parity task stages its screen into the lane; builders mine `code.html` for exacts; each deviation (design vs spec line) is CITED in the return report. |
| D2 | **User directives override design.** Guest checkout → redirect to **signup**, not the design's guest CTA, not a dead 403 wall. | W10-T2. |
| D3 | **1-hour rebrand without regression = requirement.** CMS eventual, seam now. | W14. No-hardcoded-brand-literals constraint in every handoff. |
| D4 | **Handoffs cite the vendored refs** (`.opencode/ui-ref/xias`, `.opencode/ui-ref/uupm`). | §8 citation contract. |
| D5 | Backend read-only; stays in sync by strict-sync; gaps → BACKEND_REQUESTS.md append-only. | No backend edits, ever. |
| D6 | **"Implemented live" = end state is the production storefront** (test.boredompeaks.me) carrying every rebuilt page — via RE promotion → feat/add-frontend → deploy workflow, the established path (DEPLOY-1 fixed the workflow; merges auto-deploy). | Two live cutovers mid-program (after W12, after W15) instead of one at the end. |

Hard floors that override D1 (a design cannot lower them): A-2 token
vocabulary, A-7 a11y (contrast AA, 44px, keyboard, reduced-motion), backend
contract reality A-12.D, no-new-deps A-11, LCP budget guard A-10. Where the
design shows something a floor forbids (e.g. the PDP's photographic hero was
already ruled compliant in W6-T1 — photo ban's premise died), the orchestrator
rules at dispatch, not the builder.

Two same-page design conflicts resolved at dispatch (design fights design,
not spec): verify-email has TWO screens (refined + `_standard_chrome`) —
refined is primary, standard-chrome is the fallback/empty-state reference;
checkout has one screen (Delivery step) — Review/Pay keep shipped structure
with the design's summary-rail treatment applied.

## 2. The three verdicts (unchanged from rev 1, restated)

**Q1 1hr-rebrand: POSSIBLE, bounded overhead, worth it now.** Three of four
layers exist and are audited (env→template.config, settings API, page
registry). Missing: palette/type — theme seed drives only 4 slots today.
W14 extends it to the full palette behind a **build-failing contrast guard**
(mutation-proven). Claim's honest limit: identity+palette+fonts+contact+
policy+nav; catalog content is admin-side and already CMS-shaped.

**Q2 CMS: NOT NOW, seam guaranteed.** After W14 every rebrandable value lives
in exactly three places (env vars, seed object, settings API/registry); a
later CMS swaps the source, not a component. Enforced by grep-lint test:
zero brand literals in components.

**Q3 Regressions: the gate machine holds and absorbs this program.** Rebuilding
18 pages is exactly what builder self-gate + per-wave auditor floor + Playwright
visual + mutation-proofs + RE gate exist for. Behavior (API bindings, a11y
pins, tests) is preserved; visuals are replaced. Any parity task that cannot
keep a green floor checkpoints PARTIAL rather than shipping broken behavior.

## 3. State 0 (measured this session)

**Floor:** vitest 234 pass / 23 files, build exit 0, 23 page.tsx routes
(build count 24), stylesheet 42,501 B. **Sync:** origin/spec-ui = ui-wt =
t1 = t2 = t3 = `94cb723`; feat/add-frontend (636d06b) merged backend-first,
frontend delta 0. **Contracts:** 6 MET / 2 PARTIAL / 0 MISSING / 0 DRIFT.

**Design coverage map — 16 screens, 16 routes, all exist, all get rebuilt:**

| # | Screen (designs-pages/…) | Route | Shipped as | Parity task |
|---|---|---|---|---|
| 1 | catalogue_accueil_carousel_edition | `/` | W6-T1, no carousel | **W11-T4** home full rebuild |
| 2 | all_fragrances_refined_cards_atmosphere | `/products` | W6-T2 | **W11-T5** |
| 3 | ther_s_culaire…refined_hero_flow | `/products/[slug]` | W7-T1 | **W11-T6** (biggest: hero+pyramid+philosophy+protocol+guarantee) |
| 4 | votre_panier | `/cart` | W6-T3 | **W12-T7** |
| 5 | commande_r_glement | `/checkout` | W7-T2 | **W12-T8** |
| 6 | vos_commandes | `/orders` | W7-T3 | **W12-T9** |
| 7 | order_detail_nested_tracking | `/orders/[id]` | W7-T3 | **W12-T9** (same task, ≤5 files across both) |
| 8 | connexion_sign_in | `/login` | W8-T1 | **W13-T10** |
| 9 | cr_ation_de_compte_create_account | `/register` | W8-T1 | **W13-T10** |
| 10 | r_cup_ration_de_compte | `/forgot-username` `/forgot-password` | W8-T2 | **W13-T11** |
| 11 | nouveau_mot_de_passe | `/reset-password` | W8-T2 | **W13-T11** |
| 12 | v_rification_de_courriel (+ `_standard_chrome` fallback) | `/verify-email` | W8-T2 era | **W13-T11** |
| 13 | suivi_public_de_commande | `/track-order` | W4-F4 | **W13-T12** |
| 14 | politique_de_livraison | `/shipping` | W8-T3 | **W13-T12** |
| 15 | politique_de_retour | `/returns` | W8-T3 | **W13-T12** |
| 16 | artisanal_parfumerie/DESIGN.md | (system) | token layer B1 | already binding; W14 extends slots |

**Remaining non-design queue (carried):** restock-202 P1; W9-T2 a11y
residuals; W9-T3 F1-F7 (budget re-baseline, AVIF/content-visibility, CSP
prose, dep list, 2× "4.90", PDP meta-desc empty, §10.4 prose); metadata
unification (18 pages); E2E-4 e2e; BACKEND_REQUESTS 11 still-open + 2 stale.

## 4. Ref staging (how builders see the screens)

`designs-pages/` lives untracked in the backend UI/UX checkout (read-only to
us) and media/ is gitignored — so at each parity-wave dispatch the
orchestrator stage-copies the wave's screen folders (`screen.png` +
`code.html`, ~2–6 MB) into **each lane worktree** at `media/designs/<route>/`
(gitignored, same pattern as media/res/). Builders mine `code.html` for
exact values (hexes, type sizes, spacing, section order); the spec stays
words-only executable for the record. Staging is copy-only — no source
touches, nothing committed.

## 5. Wave plan (6 waves × 3 tasks; ≤5 files, ≤1 new shared component each)

Standard mechanics (ui-wave-protocol, unchanged): lane prep `git switch -C
ui/tN spec-ui` → reverse-merge current tip INTO each lane → forward-merge
task order → backend-first conflicts → scope-slip hunks dropped →
silent-deletion check + §14 byte-check → ONE ui-auditor on ui-wt (floor +
Playwright per ui-visual-qa — **every parity wave is visual**) → SHIP =
auditor pushes origin/spec-ui → orchestrator ledger rows. Builders commit
locally, never push, never touch spec-ui directly.

### W10 — contract correctness (no visuals; unblocks honest parity work)

| Task | Scope | Gate | Refs (D4) |
|---|---|---|---|
| **T1 restock-202** | RestockForm (202→first-arm copy; stale comments :16-24), products-api (stale comment :29-33; status exposure), restock.test (+202/+200 cases) | build 0 + vitest; **mutation-prove** 202 mapping | xias `components.md` (async form states); AGENTS.md 8-states |
| **T2 guest→signup** | checkout client: 401/403 bootstrap → `router.replace("/register?next=/checkout")`; cart CTA "Checkout as guest"→"Continue to checkout"; next-param tests | build 0 + vitest; redirect pinned | xias `accessibility.md` (focus on route change); ui-taste P2 |
| **T3 a11y residuals** | RestockForm+CouponForm aria-describedby; orders/[id] h1; header 44px targets; §7.3 border spot | build 0 + vitest; §7.8 pins extended; keyboard spot | xias `accessibility.md`, `typography-and-spacing.md` (2.5.8) |

### W11 — design parity wave 1 (storefront core; staging: screens 1–3)

| Task | Scope | Gate | Refs |
|---|---|---|---|
| **T4 home full rebuild** (screen 1) | HomeCarousel (NEW shared component — manual advance only, no autoplay per P3; prev/next + progress bars + 01/04 counter per design; reduced-motion = instant swap) + home page sections per screen (hero band, new arrivals, philosophy, footer) + home.test | build 0 + vitest; mutation-prove reduced-motion; **Playwright 1280+390**: click prev/next, keyboard, focus | ui-motion (settle, LCP guard) + xias `motion-choreography.md` + uupm `motion.csv` grep; xias `components.md` carousel ARIA |
| **T5 products listing** (screen 2) | listing page + FilterBar/cards/pagination against screen (grid density, card anatomy, result count copy, ₹ tabular) — wired filters (search/category/min/max/ordering) PRESERVED | build 0 + vitest (wiring pins stay green); Playwright: filter + paginate live | ui-taste P1/P4; xias `design-taste.md`, `tokens-and-color.md`; uupm `styles.csv` grep "editorial" |
| **T6 PDP** (screen 3) | PDP sections against screen: hero + volume selector + acquire panel, olfactory pyramid (Top/Heart/Base cards), philosophy of the scent, protocol/tenue/conservation trio, guarantee band — **≤5 files: may checkpoint PARTIAL** (pyramid+hero first; second slice in W13-T12 overflow if needed) | build 0 + vitest (restock, StickyPurchaseBar, 404 pins stay); Playwright full-page both viewports | ui-taste P4 (single image), ui-motion (settle); xias `design-taste.md` (display 2.5×, 18–24ch); DESIGN.md §Olfactory Pyramids |

### W12 — design parity wave 2 (commerce; staging: screens 4–7) — **cutover #1 after this wave's SHIP+promotion**

| Task | Scope | Gate | Refs |
|---|---|---|---|
| **T7 cart** (screen 4) | CartView + CartLineItem against screen (line anatomy, qty stepper, summary rail, empty state per §5.3 designed copy) — coupon + guest CTA (post-T2) preserved | build 0 + vitest; Playwright: qty ±, remove, coupon form | xias `components.md` (line item); ui-taste P2 |
| **T8 checkout** (screen 5) | CheckoutView against screen: stepper 01/02/03, delivery form open-hairline fields, summary rail, order-reference eyebrow — Review/Pay keep shipped logic, design's chrome | build 0 + vitest (checkout.test 16/16 pins stay); Playwright: step walk + focus-move pin | xias `accessibility.md` (step focus), `components.md` (stepper) |
| **T9 orders list + detail** (screens 6–7) | OrdersListView + OrderDetailView against screens (status chips, six-timestamp journey, nested tracking panel) — anonymous walls verbatim, order_number consumed | build 0 + vitest; Playwright: authed fixture walk | xias `design-taste.md`; ui-taste P5 (real numbers) |

### W13 — design parity wave 3 (account + policy; staging: screens 8–15)

| Task | Scope | Gate | Refs |
|---|---|---|---|
| **T10 login + register** (screens 8–9) | LoginForm + RegisterForm against screens (centered card, brand mark, link stack) — next-param chain, TOTP-absent customer door, danger-not-bronze preserved | build 0 + vitest; Playwright keyboard pass | xias `accessibility.md`; ui-taste (forms: labels outside, hairline) |
| **T11 recovery family** (screens 10–12) | forgot-username/forgot-password/reset-password/verify-email clients against screens (3 reset states, verification copy; standard-chrome screen = fallback reference) — verbatim server copy preserved | build 0 + vitest (3-state pins stay); Playwright: state walk | xias `components.md` (form states) |
| **T12 tracking + policies** (screens 13–15) | track-order page + shipping + returns against screens (public tracking panel, policy typography, anchor TOCs) — noindex/canonical pins, no returns FORM (§14 unchanged) | build 0 + vitest; Playwright; robots/sitemap parity | xias `typography-and-spacing.md` (measure); ui-taste P1 |

### W14 — white-label completion (D3; the 1hr-rebrand + CMS seam)

| Task | Scope | Gate | Refs |
|---|---|---|---|
| **T13 seed-full-palette** | theme.ts full slots (paper/ink/ink-secondary/hairline/danger/success/focus; DEFAULT_SEED = today's values), theme.test contrast guard (focus≥3:1, body/danger/success≥4.5:1; **mutation-proven ×3**), globals.css slot consumption, layout.tsx wire | build 0 + vitest; **zero visual delta proof** (computed-style spot on rebuilt pages) | xias `tokens-and-color.md` + contrast doctrine (script absent → equivalent in theme.test.ts); uupm `colors.csv` grep |
| **T14 rebrand-smoke + runbook** | rebrand.test (seed+env swap → zero brand literals, every slot resolves, nav/footer derive), TEMPLATE.md 1hr checklist | build 0 + vitest; auditor dry-runs checklist on scratch values (not shipped) | xias AGENTS.md "one theme one source"; `brand-and-operations.md` |
| **T15 budget re-baseline + residuals** | §10.2 honest targets (framework floor 194.8 KB measured; increment cap kept; css projection updated for parity waves), sitemap.ts:7-9 stale comment, F3/F4/F5/F7 | doc-ONLY (ui-cycle-cost); claim-scan every number | ui-audit-protocol Rule 3; xias AGENTS.md verification |

### W15 — e2e + images + overflow — **cutover #2 (final) after SHIP+promotion**

| Task | Scope | Gate | Refs |
|---|---|---|---|
| **T16 e2e-align (E2E-4)** | 2 specs: fixture prices → seed truth, coupon math recomputed, Razorpay wallet steps `test.skip` unless RAZORPAY_TEST=1 | playwright w/ backend up: green + proven skip marker | spec §9.2/§12-G8; xias "Render and look" |
| **T17 F2 images** | AVIF where next/image allows, content-visibility on below-fold bands, §10.4 numbers refreshed | build 0 + vitest; LCP spot (throttled Playwright once) | ui-motion LCP guard |
| **T18 overflow / final** | any PARTIAL backfill (e.g. PDP second slice from T6), metadata batch-2 (≤5 pages), residual sweep, ledger hygiene | build 0 + vitest | — |

**RE train math:** 3/9 now. W10→6/9; W11→9/9 **GATE → promotion #3 → cutover
live (D6)**; W12→3/9; W13→6/9; W14→9/9 **GATE → promotion #4**; W15→3/9
**cutover #2 live**. 18 tasks, 2 live cutovers, master untouched throughout.

## 6. What "done" looks like (verification per page, post-SHIP)

For each of the 16 routes, the auditor's Playwright record shows: (a) 1280 +
390 screenshots archived under `frontend/test-results/audit-waveN/`; (b)
every control clicked with a visible state change; (c) keyboard tab pass with
the focus ring on every interactive element; (d) reduced-motion pass (no
motion, no content lost); (e) behavior pins green (filters, qty, coupon,
steps, next-param, order chips); (f) the parity citation list (design vs
spec deviations) attached to the wave's ledger row. Live check after each
cutover: orchestrator hits test.boredompeaks.me + the 16 routes, spot-checks
HTTP 200 + title + one screenshot each, records in ledger.

## 6b. GATE-U — periodic user-persona live audit (owner directive, rev 2)

A **general subagent with a user identity** (frontend-technical shopper, not
an auditor/builder) explores with Playwright and returns a RATING + critique
list. **Every rating is surfaced to the owner; the owner triages each
critique into implement / reject — no critique becomes work without the
owner's word.**

**When (at every gate + periodically):**
1. **Baseline (first run):** against the DEPLOYED storefront, now, before W10.
2. **Every wave gate:** after the wave's auditor floor is green, before SHIP —
   persona runs the same flow against the local build wired to the real
   backend (`api.boredompeaks.me`), so the rating travels with the gate.
3. **Every cutover:** after promotion #3 and #4, against test.boredompeaks.me
   (deployed) — the owner's exact live checklist.

**The flow (owner's checklist, verbatim):**
```
Open the deployed storefront.
Load a product using the real backend.
Verify the displayed price against the backend's authoritative data.
Add the product to the cart.
Verify cart contents and totals.
Exercise the intended checkout authentication flow.
Verify the resulting behavior, including the redirect to registration.
Capture failed requests, console errors, and the precise step that failed.
```
Plus: auditor-style credential use (discover seeded creds in frontend/e2e/;
else register/login via UI — verification walls are recorded as findings,
not worked around), and a **pristine-visual pass** at 1280 + 390 with
reduced-motion emulation.

**Pristine = "no harsh anything":** (a) no pure #000/#fff harshness (slop
tell; ink is #1c1c18 on paper #fcf9f3); (b) no snap transitions (0ms/default-
easing where the design mandates the settle curve); (c) no contrast below AA
on any measured pair; (d) no heavy/harsh borders (hairline = 1px #e5e2dc);
(e) no horizontal overflow at 390; (f) focus ring visible on every
interactive element. **OPEN AMBIGUITY flagged to owner:** "no hard 0px" was
read as harshness (black/white/snap), NOT as abandoning the radius-0 design
mandate — the design system requires 0-radius corners; if the owner means
softening corners, that is a design-system change needing a spec amendment,
and 0-radius critiques are parked until ruled.

**Output contract:** overall /10 + 6 subratings (pristine-visual,
price-integrity, cart, checkout-auth, console/network, taste-vs-refs), each
with measured evidence; step log PASS/FAIL with the precise failing step;
failed requests + console errors captured from first navigation; numbered
critique list (tagged PARITY/BUG/TASTE/PERF/A11Y, each with page, severity,
evidence path, fix sketch); screenshots archived
`frontend/test-results/gate-u-<run>/`. Ratings are opinion, stated as
opinion; every number is measured (ui-audit-protocol Rule 3). Handoff cites
the ref contract of §8.

**Register rule (owner, 2026-10-17):** critiques are NEVER dispatched
mid-run. Each run appends its block to `frontend/docs/run/GATE-U-REGISTRY.md`
(append-only; owner-ruling on GATE-U-0 = park all 16). Waves proceed
uninterrupted — "complete the plan before the reviewers". At plan end a
compliance pass strikes (✅) items the waves already fixed, then the owner
rules on the remainder. GATE-U still RUNS at every gate and appends its
rating + critiques; it just never diverts the wave queue.

## 7. Merge/sync protocol (unchanged)

Lane prep → reverse-merge tip into lane → forward-merge t1→t2→t3 →
backend-first conflicts (ambiguity = escalate, never silent) → scope-slip
hunks dropped pre-audit → `diff-filter=D` empty + per-lane markers → §14
byte-check (`cmd` redirect, never Out-File) → auditor → push → ls-verify →
ledger. Backend worktrees observed read-only. `git push` from orchestrator
only via the AUDIT-W5 cmd-wrapper precedent (harness deny), fast-forward
only, ls-remote verified.

## 8. Handoff citation contract (D4)

Every builder payload names exactly: (1) spec section range + the staged
screen path; (2) 2–3 binding xias lines (rules file + one taste line +
one AGENTS.md non-negotiable); (3) the one uupm CSV to grep for that task's
domain; (4) ui-taste/ui-motion named with 3 binding lines; (5) ui-cycle-cost
gate list; (6) `backend/** read-only; gaps → BACKEND_REQUESTS.md`.
Vendored-ref honesty: xias ships doctrine, NOT its gate scripts; uupm ships
CSVs, NOT search.py — runnable equivalents are our vitest pins + Playwright.
Vendoring the scripts = optional small INFRA task, owner say-so.

## 9. Projected numbers (projections — every SHIP number is re-measured)

| Gate | Now (94cb723) | End of program |
|---|---|---|
| vitest | 234 / 23 files | **~320–360 / ~30 files** (parity pins + carousel + seed/contrast + rebrand + e2e-adj) |
| build | 0 | 0 (fail-if-unset guards intact) |
| routes | 24 | **24** (no new routes — every screen has one) |
| css prod | 42,501 B | **~46–52 KB** (carousel + pyramid + parity sections; @source allowlist re-checked every wave, BUG-10 regression pinned) |
| e2e | 1 pass / 2 fail / 2 skip | green with payment env-skipped |
| visual records | W9 last | 16 routes × 2 viewports × 2 cutovers archived |
| live | current site | **all 16 design pages live at test.boredompeaks.me** (cutover #1 after W12-promotion, #2 after W15) |

## 10. Explicit non-goals

No CMS build (seam only); no backend edits; no new deps (carousel + pyramid
are CSS+React only — no swiper/embla/three); no native select, emoji,
gradient, radius creep, one-sided borders (design system + AGENTS.md floors);
no one but release-engineer pushes/merges; master frozen. Currency stays ₹
per R4c (screens are INR; the two €-mockup strays in the PDP/cart crops are
mockup drift, ruled at dispatch — spec + majority of screens = INR).

## 11. What I need from you

One word: **go**. On go: W10 dispatches within the hour; parity staging
starts with W11; waves run continuous per standing rule; this file commits
as plan-of-record with the go timestamp. Plan edits before dispatch — tell me.
