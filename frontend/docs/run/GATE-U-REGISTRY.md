# GATE-U critique register (append-only)

Owner rule (2026-10-17): GATE-U critiques are **never dispatched mid-run**.
Every run appends its block below with the rating. Waves proceed per
WAVE-PLAN.md regardless. At plan end a compliance pass checks each open item
against the shipped tree and **strikes (✅) what the waves already fixed**;
the owner then rules on the remaining struck-through/open items: take into
consideration or dismiss.

Lifecycle: `OPEN` → `✅ STRUCK-DONE (compliance-verified)` or `OWNER-SKIP`.
Only the ui-orchestrator edits this file (append-only per run; strike-off in
place, never renumber, never delete a row).

---

## Run GATE-U-0 — baseline on deployed `test.boredompeaks.me` — 2026-10-17
**Rating: overall 6/10** (pristine 8 · price 10 · cart 9 · checkout-auth 2 ·
net/console 6 · taste 7). Report: `frontend/test-results/gate-u-baseline/REPORT.md`
(41 screenshots). Owner ruling on this block: **park all — none dispatched.**

| ID | Tag | Sev | Page | Critique | Evidence | Status |
|---|---|---|---|---|---|---|
| U0-01 | BUG | P1 | /checkout | Order creation dead on CORS: preflight rejects `idempotency-key` (probe: with header → fetch throws; without → reaches server 403 CSRF). Backend must add it to `CORS_ALLOW_HEADERS` (real spelling — `CORS_ALLOWED_HEADERS` is silently ignored) | gate-u-baseline `step-8_after_create_order_1280.png` | OPEN |
| U0-02 | PARITY | P1 | /checkout | Intended `/register?next=/checkout` redirect absent (W10-T2 unbuilt); guest form shown, wall appears at Create-order with no escape | `step-7_checkout_anon_1280.png` | OPEN |
| U0-03 | BUG | P2 | /checkout | Delivery-estimate panel never leaves empty state: backend returns `options:[]`, frontend has no "no options configured" state | `step-8_checkout_filled_1280.png` | OPEN |
| U0-04 | BUG | P2 | /login | Unverified account indistinguishable from wrong credentials (uniform 401 copy); no inline resend-verification | `step-9_login_unverified_1280.png` | OPEN |
| U0-05 | A11Y | P2 | /checkout /cart | Error copy wears bronze accent `#8c5338` (measured rgb 140,83,56), not the danger token — token-by-intent (or owner rules deliberate → spec exception) | `step-8_checkout_validation_1280.png`, `step-5_coupon_invalid_1280.png` | OPEN |
| U0-06 | A11Y | P3 | /checkout | No error summary, focus not moved after failed submit (uupm ux-guidelines row 109) | measured activeElement stays on button | OPEN |
| U0-07 | TASTE | P2 | /products | Native `<select id="sort">` (xias AGENTS.md hard ban): needs button trigger + ARIA listbox | measured: 1 select on page | OPEN |
| U0-08 | TASTE | P3 | site-wide | Off-token transitions: default `cubic-bezier(0.4,0,0.2,1)` sampled on 10+ elements instead of settle curve `--motion-ease` | computed styles PDP/cart | OPEN |
| U0-09 | TASTE | P3 | inner pages | Display type 2.25× body (rule ≥2.5×); mobile PDP/login h1 1.79×; home hero passes 3.5× | h1 36px / body 16px measured | OPEN |
| U0-10 | TASTE | P3 | /products/[slug] | Product media 83.8vh vs 60vh screen-economy cap | media box 670px @800vh | OPEN |
| U0-11 | TASTE | P3 | / | 625px dead gap under single product card (grid reserves multi-card space) | card bottom y=1240 → footer y=1865 | OPEN |
| U0-12 | TASTE | P3 | /, /checkout, /shipping, /returns | 10 em-dashes in UI copy (xias machine-tell); en-dash numeric ranges exempt; zero filler verbs found | count per page | OPEN |
| U0-13 | PERF | P3 | site-wide | favicon 404; Cloudflare beacon injected but CSP-blocked every nav; 2 woff2 preloads flagged unused per nav | console + network log | OPEN |
| U0-14 | BUG | P3 | /orders (anon) | Anonymous token-refresh fired → 400 console error | network log | OPEN |
| U0-15 | PARITY | P2 | /products/[slug] | Backend placeholders rendered as content ("Fragrance notes: None", `image:null` → grey letter panels) | `step-2_pdp_oud_1280.png` | OPEN |
| U0-16 | TASTE | P3 | /shipping /cart /checkout | Three conflicting delivery-cost sentences | copy diff | OPEN |

Run-block template for future runs:

```
## Run GATE-U-<n> — <target> — <date>
**Rating: overall X/10** (six subratings). Report: <path>.
| ID | Tag | Sev | Page | Critique | Evidence | Status |
| U<n>-01 | ... | ... | ... | ... | ... | OPEN |
```
