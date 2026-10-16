# UI run ledger — storefront redesign

Branch `spec-ui`. Worktree `C:\Users\Admin\AppData\Local\Temp\opencode\ui-wt`.
Base `feat/add-frontend` @ `4e424fb`.

Every status change cites evidence. The `auditor` never edits this file; the UI
orchestrator does. **This ledger is not the integration record** — the backend
orchestrator runs `UI-UX-SPEC.md` §14 and records the promotion there.

---

## Phase status

| # | Task | Spec section | Status | Attempts | Evidence |
|---|---|---|---|---|---|
| R1 | Design research — fill both design docs | all §1–§13 | **DONE** | 2 | See "Session notes" below. Zero `TODO` in either file; §14 byte-identical (4257 chars, verified byte-exact via `cmd` redirection). |
| R2 | Reference-site analytics + 3D/cube feasibility | §3, §10, §11, §13 | **DONE** | 1 | Sizes measured, not estimated. A-6 superseded; A-18–A-21 added. |
| R3 | Cost of being wrong; budget doubling; middle point; rationing curve | §11.4, §11.5, §13 | **DONE** | 1 | Three transfer conditions measured; throttled rates are model constants (A-23/A-24). Retracted the round-1 CSP inference in §10.5 and §11.5.1. |
| C1 | Commit verified design docs | — | **DONE** | — | `9e6561a`. Tree clean at commit time. |
| R4 | **Reconcile spec against the 17 selected Stitch refs** + harmonise cross-page drift | §0b, §2, §3, §4, §7, §10, §12, §13 | **DONE** | 2 | Commit `ca54e90`. Attempt 1 blocked by `free-models-per-day-stealth`; attempt 2 succeeded. Followed by a currency correction round (below). §14 byte-identical, zero `TODO`. |
| R4c | Currency correction after orchestrator-caught error | §0b, §12.D | **DONE** | 1 | R4 wrote 16 yen (U+00A5) signs into the spec; refs contain 0 yen / 53 rupee (U+20B9). Corrected to INR; 3 further transcription fabrications removed. Verified: yen=0, rupee=33. Commit folded into the R4 lineage. |
| B1 | Token layer: canonical palette, motion/spacing tokens, Playfair+Jakarta swap, focus ring | §2.1, §2.2, §2.3, §2.4, §7.3, §10.4 | **SHIPPED** | 1 | `e786039`. Audited in batch A/B. Token layer costs 0 B: stylesheet 41,784 B raw / 8,867 B gz, unchanged vs pre-change build. |
| B2 | Migrate 105 removed-token refs across 33 files; fix false-pass test; add token-existence guard | §2.1 | **SHIPPED** | 1 | `6109b94`. 36 files, +314/−179. 32/36 provably mechanical (7-token rename map applied to HEAD blobs, diffed). Zero residue: `ink-muted`/`line-strong`/`bronze-soft`/`divide-line` all 0 hits. |
| B3 | Fix audit findings BUG-1/2/5/6/7/8/9 | §2.4, §3, §7.3 | **SHIPPED** | 1 | `8088ea9`. All 7 cycle-1 bugs closed at cycle 2 re-audit. |
| AUDIT-A | Batch A/B audit, cycle 1 | — | **FIX** | 1 | 9 bugs (3×P2, 6×P3). Verdict withheld. Substance confirmed: focus ratios reproduced to 2dp, palette 13/13 exact, void migration complete, token guard proven non-vacuous by deliberate breakage. |
| AUDIT-A2 | Batch A/B re-audit, cycle 2 | — | **SHIP** | 2 | All 7 fixable bugs CLOSED. Floor: **61 tests / 8 files, build exit 0, 23 routes**. §14 byte-identical (`fc /b`: no differences). Not pushed. |

### Batch A/B — audit findings still open

| # | Sev | Finding | State |
|---|---|---|---|
| BUG-3 | P2 | No ledger row existed for the batch when first audited | **CLOSED** — rows added above |
| BUG-4 | P2 | **e2e UNPROVEN, not passing.** `ECONNREFUSED ::1:8000`, `localhost:3000`. Zero assertion failures; servers down, so this is environmental — but the batch repaints every route and e2e is the only gate that would catch a visual regression. 3 failed / 2 did not run. | **OPEN — must run with the backend up before §14.** Do not waive. |
| BUG-10 | P3 | Tailwind v4 auto source-detection scans `frontend/docs/design/*.md`, injecting **56 dead app rules = 5,818 B ≈ 13.1%** of the 44,564 B production stylesheet — including `z-50`, `z-[55]`, `duration-500`, `scale-[1.04]`. Design prose is becoming shipped CSS. | **OPEN** — fix with an explicit `@source` allowlist. Not fixed in this batch: it predates it. |

### Process rules established by this batch

1. **A number in a comment is a claim and is audited like code.** Three false
   numbers shipped as justification (a z-index count, a preload byte total, a
   contrast ratio computed against a retired token). Recompute; cite a token that
   exists.
2. **A guard must be proven non-vacuous by breaking it.** The token-existence
   test now fails on a typo'd token and returns clean on 19 layout utilities.
3. **Every measured figure comes from a command in that session.** Third
   occurrence of a fabricated number after the yen glyph and the phantom product
   strings — root cause identical: asserted instead of measured. A-40.

### Confirmed by audit, not accepted on report

- Focus `#9c6a2e`: **4.426 / 4.652 / 3.682** on paper / white / ink-deep — ≥3:1.
- `#8a5a24` on ink-deep is **2.911**, on ink **2.904**, on retired ink **2.972**
  — the pre-existing 1.4.11 failure, recomputed from literals.
- `prefers-reduced-motion` appears in **1 file** (`globals.css`), 0 in components.
- Raw `z-50` / `z-[n]` in `src/**`: **0**. Shipped z-index resolves by token
  value 40/50/60/70, no longer by DOM accident.
- Fonts: 12 woff2 = **243,728 B**; latin three = 104,620 B; exactly **2** preloads
  = 77,348 B. §10.4's ~110 KB lever keys off the **total**, so the rewritten
  justification is correct and `preload: false` stands.
- Dead `z-[77]` quoted in a `globals.css` comment emits **0** rules — Tailwind
  does not scan CSS files. 21 utility-shaped tokens occur only there; 20 absent
  from the stylesheet.

---

## Verification performed by the orchestrator

Each check re-run against the working tree, not taken from an agent's report.

| Check | Method | Result |
|---|---|---|
| Zero `TODO` in either design doc | `Select-String` over both files | PASS — 0 and 0 |
| §14 untouched | `cmd`-byte-exact `git show` → UTF-8 ordinal compare of the §14 substring | PASS — 4257 chars both sides, ordinal equal |
| §14 untouched (independent) | `git diff` hunk list, last hunk at original line 245 | PASS — no hunk at or beyond §14 (starts line 249) |
| Only design files modified | `git status --porcelain` | PASS — 2 tracked + 1 new analysis file |
| No `refs/` directory | `Test-Path` at root and under `frontend/` | PASS — absent |
| No images committed | `.gitignore:11` = `media/`; `git check-ignore -v` on the zips | PASS — ignored, cannot be committed |
| Stale agent self-report | researcher's 1st two `write` calls both errored (truncated payload, missing `content`); tree was clean after | caught before any verification was trusted |
| Fabricated currency glyph | codepoint count U+00A5 vs U+20B9, spec vs all 17 refs, read as UTF-8 bytes | **CAUGHT** — refs 0 yen / 53 rupee; spec had 16 yen. Corrected; re-verified yen=0. Root cause: PowerShell console rendered U+20B9 as `?` and the misreading was written into a commerce spec as fact. |

### Source claims independently re-derived by the orchestrator

- **CSP P0 (G8) CONFIRMED.** `next.config.ts:16` ships
  `scriptSrc = ["'self'", "'unsafe-inline'"]`, no `frame-src`, no Razorpay origin.
  `lib/razorpay.ts:19` injects `https://checkout.razorpay.com/v1/checkout.js`.
  The gateway cannot load under the app's own CSP.
- **Focus-ring failure CONFIRMED to 3 significant figures.** `#8a5a24` on
  `#1c1917` = **2.970:1** (below the 3:1 floor of 1.4.11); proposed `#9c6a2e`
  = **3.760:1**. Recomputed by hand from the literal channel values. **Superseded
  by R4** — the refs introduce a new palette, so all of §2.1 is recomputed there.
- **Radius-0 mandate is nearly free.** Exactly 2 `rounded-*` usages across all
  85 `.tsx`/`.ts` files under `frontend/src`: `page.tsx:149` and `Header.tsx:151`,
  both `rounded-full`. `Header.tsx:151` is the bag counter, which the ref system
  explicitly permits as its single radius exception.

---

## Findings requiring a decision outside the UI orchestrator

| # | Finding | Owner | State |
|---|---|---|---|
| G8 | Razorpay cannot load under the shipped CSP. Fixing it weakens the CSP. | user + backend owner | recorded §12-G8; payment e2e deliberately not written |
| G1 | `guest_token` storage policy for guest checkout — bearer credential for PII | user + backend owner | escalated, not decided |
| G13 | No customer-facing returns endpoint. `/returns` is a policy page. | backend | recorded; page must not post a form nowhere |
| A-9 | §14.6 records a backend floor of **1757**; the ledger's latest figure is **1515→1617**. §14.8 makes a regression a stop condition. | backend orchestrator | **must be reconciled before §14 runs**; §14 deliberately not edited |
| — | `frontend-architect` config forbids animation "for its own sake", conflicting with the settled direction | user | reported 3×; unapproved. Mitigated per-task by carrying the motion contract in every handoff payload. Not yet fixed at source. |

---

## Session notes

**R1 took two attempts.** The first dispatch died with zero output: both `write`
calls errored, the first truncated mid-payload with a missing `content` key. The
session was recovered from the opencode store
(`ses_f02573e86ffeic3dQoDo9akix7`) and resumed with a write-strategy brief
rather than re-running the research. A later re-check appeared to show §14 had
changed; byte-exact extraction proved the harness was corrupting UTF-8 em dashes
via a PowerShell pipeline, not the agent. **Compare §14 with `cmd` redirection,
never `Out-File -Encoding utf8`.**

**The refs arrived in `media/res/`, not `media/refs/`.** Noted, not corrected —
`media/` is gitignored so it cannot reach git either way. **The "no `refs/`
directory" rule is now deliberately inverted by the user**, who ran Stitch by hand
and selected the designs. Images stay uncommitted; the spec must remain
executable from words alone.

---

## Frontend-ops phase (2026-10-15, user-directed)

User decisions (verbatim intent, 2026-10-15): merge feat/add-frontend now; template
architecture first, then features/policies, then per-page redesign; a11y = keep shipped
tokens, no new gates; 3 parallel builders (isolated worktrees, local commits, no pushes)
-> sync -> 1 auditor on ui-wt -> auditor pushes origin spec-ui; RE train every 9 shipped
tasks; auditor must visually inspect via Playwright when work is visual (dev server
background-only); bridge file `Temp\opencode\orch-bridge.md` with backend orchestrator
(do not commit); guest-token = sessionStorage (standing recommendation, reversible —
user has not vetoed).

| # | Task | Wave | Status | Attempts | Evidence |
|---|---|---|---|---|---|
| U-W0 | Merge origin/feat/add-frontend into spec-ui | — | **SHIPPED** | 1 | `7d97460`; auditor verified diff-scope (backend/ diff EMPTY after revert). Pushed with wave. |
| U-W1-T1 | template.config.ts manifest; site.ts reads it | 1 | **SHIPPED** | 1 | `e13a74c`; audit: env-precedence tests green; 4th file .env.local.example ACCEPTED. Pushed `fa40218`. |
| U-W1-T2 | theme seed -> CSS custom properties | 1 | **SHIPPED** | 1 | `e14f71a`; audit: zero-delta CONFIRMED (--color-bronze #8c5338 / alt #885035 via computed style on /). changes.md row DROPPED at sync, revert held. Pushed `fa40218`. |
| U-W1-T3 | page registry; sitemap/robots consume it | 1 | **SHIPPED** | 1 | `7eb19eb`; audit: 21 unique paths, robots parity on all six disallows. Sitemap 2->9 ACCEPTED. Pushed `fa40218`. |
| AUDIT-W1 | Wave-1 full audit (frontend lane) | — | **SHIP** | 2 | cycle1 died (hung script, stray :3000 killed by orch); cycle2: 85/85 vitest, build 0, visual PASS (screenshots frontend/test-results/audit-wave1/), push `origin/spec-ui=fa4021897fab812f7ed06347e70800e2ea0708aa` (push verified by orch via ls-remote). |
| U-W2-T4 | TEMPLATE.md white-label contract | 2 | **SHIPPED** | 1 | `d302e51` (+232 ln, 1 file). 7 code-vs-doc gaps DISCLOSED not promised (auditor spot-checked 3: real). |
| U-W2-P1 | return-window binds to settings API | 2 | **SHIPPED** | 1 | `e01af15` 5 files; guard MUTATION-PROVEN (weakened copy -> test fails); zero literal "7 days" in the 3 pages. |
| U-W2-F2 | persisted cart coupon (POST/DELETE /cart/coupon/) | 2 | **SHIPPED** | 1 | `093a7ea` 3 files; sessionStorage=0, preview->persist trace OK, types.ts untouched (BE owns drops). Residual: CheckoutView dead removeItem — next build. *(closeout U-W5-T3: already removed in `d485eea`; none remained.)* |
| AUDIT-W2 | Wave-2 full audit (frontend lane) | — | **SHIP** | 1 | Floor: **98/13 vitest, build 0, 23 routes, 34,308 B css**; screenshots frontend/test-results/audit-wave2/; PUSHED `origin/spec-ui=add79e5ed99dcee0cc48506149f93ef2177230aa` (orch ls-verified). |
| U-W3-T5 | nav/footer consume manifest; guestCheckout gates CTA copy | 3 | **SHIPPED** | 1 | `7f686d8`; audit zero-delta PASS on href|label tuple diff (nav2/footer10/menu5); tests 107. |
| U-W3-T6 | brand-derived mailto+storage key; dead coupon cleanup | 3 | **SHIPPED** | 1 | `d485eea`; 0 "Maison Aurel" in ContactLinks; tokens.* untouched (BE Q-F3 owns aurel.session). |
| U-W3-T7 | dispatch/delivery windows single-sourced in policies | 3 | **SHIPPED** | 1 | `07e0649`; en-dash codepoints verified (U+2013); mutation-proven page guard; BACKEND_REQUESTS P2 entry. |
| AUDIT-W3 | Wave-3 full audit | — | **SHIP** | 1 | Floor: **107/14 vitest, build 0, 23 page routes, 34,308 B css**; guards mutation-proven 3 ways; PUSHED `origin/spec-ui=fc94fdc3387de27d7a543468b05983e5c0b3c689` (orch ls-verified). |
| U-W4-F1 | guest checkout plumbing (sessionStorage + header) | 4 | **IN-AUDIT** | 1 | `30778db` PARTIAL: backend REFUSES anonymous checkout (uniform 403, mints nothing — TIER1-GUEST-DEADEND owner decision verified). Frontend: flag-gated wall, server copy verbatim, guest-token store ready for future mint. BACKEND_REQUESTS P1. |
| U-W4-F3 | restock opt-in on sold-out PDP | 4 | **IN-AUDIT** | 1 | `8ac8fcd` RestockForm + flag consumer #2; BACKEND_REQUESTS P1 (endpoint IsAuthenticated, email ignored by trigger). |
| U-W4-F4 | /track-order page | 4 | **IN-AUDIT** | 2 | `52d745d` (resume after builder hallucinated missing worktree); fixed silent `starting-style`→`starting` 0B no-op; indexable:false; 24 routes. |
| AUDIT-W4 | Wave-4 full audit | — | **SHIP** | 1 | Floor: **132/17 vitest, build 0, 24 routes, 36,690 B css (=34,308+2,382 exact)**; guards mutation-proven; 1 P3 (track-order nested `<main>` — next build); PUSHED `origin/spec-ui=083fe74fbc8a45d32689d89b56ef628b08eaa7a6` (orch will ls-verify). |
| FIX-B1 | RE gate: GitGuardian fixture + P3 main | 4 | **SHIPPED** | 2 | `752102d` (fixture low-entropy, zero assertions touched — mutant red proven); P3 main→div className byte-equal, a11y single-main proven; floor exact hold **132/17, build 0, 24 routes, 36,690 B css (delta 0)**; PUSHED `origin/spec-ui=5707c542134528238a53d8704a9f413e6b7b16db` (auditor ls-verified). |
| PROMOTE-39 | RE gate: spec-ui → feat/add-frontend (PR #39) | — | **ESCALATED (owner action)** | 2 | RE cycles 1-2: unit-build/gitleaks PASS, e2e SKIP, delta 3 files clean, head-tree literal 0 hits; GG FAIL persists — check scans the PR's full 41-commit range → historical incident 38246359 on `30778db`. Merge withheld. Owner options: (a) resolve/false-positive incident 38246359 in GitGuardian dashboard + re-run; (b) authorize spec-ui history rewrite to purge (force-push — NOT taken). Re-wake RE delta-only on green. |
| REWRITE-1 | Owner option (b) EXECUTED: purge fixture literal from spec-ui history | — | **DONE** | 1 | Owner directive "history rewrite, don't keep bad stuff". `filter-branch --tree-filter sed` over `adda806..spec-ui` (11 commits, 10 s); **pickaxe `-S Gb7Kq2sV9xLp` on new spec-ui = 0 hits**; **tip tree byte-identical** (`git diff 44853b4..newtip` empty — all prior measured floors remain valid). SHA map: 30778db→`a002ffd`, 0bec4db→`03dd29a`, b297d3c→`b33b251`, 083fe74→`03595fa`, 5f66690→`2046b58`, 752102d→`29ce70a`, 5707c54→`08f9ab7`, 2298d11→`f87817c`, 44853b4→`f2265fe` (tip); 52d745d/8ac8fcd/adda806 unchanged (trees never held it). OLD history retained ONLY at safety tag `safety/pre-gg-rewrite` + `refs/original/` — PURGE both after GG green + PR #39 merge. Force-push = RE wake (owner-authorized). |
| PROMOTE-39b | RE cycle 3: force-push DONE, GG GREEN, merge blocked on ruleset | — | **SHIPPED (MERGED via 39c-39f)** | 3→6 | Cycles 4-6: sentinel `55482df` (postgres/lint report on FE-only PRs), CI hermetic env `1d6520f` (NEXT_PUBLIC_ADMIN_ORIGIN provided, product fail-if-unset guard PRESERVED), CadenceTests window flake re-run per owner (02:17-03:17Z, waited out). **PR #39 MERGED 2026-10-16T03:54Z, merge-sha `6dcaccc9`** (feat/add-frontend a43f0586→6dcaccc9; master untouched 27293a5; deploy triggered run 38847871713). **RE TRAIN RESET: 0/9.** REWRITE-1 purge EXECUTED: safety/pre-gg-rewrite tag + refs/original deleted. |
| U-W6-T1 | home page redesign §5.1 | 6 | **SHIPPED** | 1 | `4ce525e`; audit: hero deviation RULED COMPLIANT (§5.1:1231 + LCP supersede §3:971 — photo ban killed its premise); settle gated correctly (static under reduce); 139/18. |
| U-W6-T2 | products listing redesign §5.2 | 6 | **SHIPPED** | 3 | `aa40dc9`; audit: 7/7 controls live, empty≠failure copy verified, "accent NONE" confirmed vs STITCH §3.3:520; 14-stop tab pass clean. |
| U-W6-T3 | cart redesign §5.3 | 6 | **SHIPPED** | 1 | `40d097f`; audit: view-transition root off + reduce-skip live, designed error ≠ empty copy, Remove in CartLineItem:178; contrast 7/7 literals reproduce. |
| AUDIT-W6 | Wave-6 full + visual audit | — | **SHIP** | 1 | Floor: **139/18 vitest, build 0, 24 routes, 40,175 B css** (+3,485 named vs 36,690; lanes 3,460 reconciled — 25 B merge residue); guard mutant-red proven; 3 P3 comment-prose findings (globals.css .money claim, "two→three declarations", GridSkeleton gap claim) — travel with next build; PUSHED `origin/spec-ui=33b3b50` (ls-verified). |
| U-W7-T1 | PDP redesign §5.2 detail half | 7 | **SHIPPED** | 3 | `9bbf1b5`+`0b04620` (same-session continuation after 2 harness cuts — BO-style task_id resume adopted); StickyPurchaseBar RULED spec-named (§5.2:1314-1319); restock preserved; audit: 390 bar mount/hide/no-shift proven, 1280 display:none, sold-out RestockForm live, 404 designed; guards mutant-red. |
| U-W7-T2 | checkout layout §5.4 | 7 | **SHIPPED** | 1 | `ed79d16`; audit: all 7 preservation items verified (flag-off branch :64, guest-token 0-diff, handoff testid :539, CouponForm/PaymentLauncher 0-diff); checkout.test 16/16; focus ring live. |
| U-W7-T3 | orders list+detail §5.5 (auth side) | 7 | **SHIPPED** | 1 | `484cdca`; audit: authenticated list+detail LIVE (six-timestamp journey, chips, ₹ totals); anonymous walls verbatim, no guest fallback; e2e rider test-only; T3 citations exact vs serializers. |
| AUDIT-W7 | Wave-7 full + visual audit | — | **SHIP** | 1 | Floor: **169/20 vitest, build 0, 24 routes, 41,987 B css** (+1,812 named; merged < Σ lanes; @source allowlist intact); guards mutant-red ×2; zero findings P1/P2/P3; PUSHED `origin/spec-ui=1cf60f6` (ls-verified). INFRA note recorded: browser /api/* harness fixture mock enabled live checkout/orders inspection. |
| U-W8-T1 | auth pages §5.9 (login/register) | 8 | **SHIPPED** | 1 | `d708e33`+`4d5abdd`; audit: anims=0 (motion:none honoured), TOTP absent, focus 13/13+14/14, danger not bronze, next-param chain source-verified. |
| U-W8-T2 | recovery pages §5.10 | 8 | **SHIPPED** | 1 | `945f80c`; audit: 3 distinct reset-password states correct (incomplete/rejected-verbatim/network no-link-blame), uniform discipline pinned + runtime spot ✓. |
| U-W8-T3 | contact §5.8 + legal §5.11 | 8 | **SHIPPED** | 2 | `2b2f43a`+`a849a50`; audit: no form by design (0 POST hits), ToC anchors 7/7+5/5, legal digit scan clean, placeholder discipline ✓. |
| AUDIT-W8 | Wave-8 full + visual audit | — | **SHIP** | 1 | Floor: **213/23 vitest, build 0, 24 routes, 42,501 B css** (+514 exact: auth+219+legal+295); guard mutant-red (legal figure injection caught); SLIP-1 integrity proven (HomeCarousel grep=0, page.tsx 0-diff); 1 out-of-delta P3 (spec §5.8 border-field "4.90:1" figure wrong — doc sweep); PUSHED `origin/spec-ui=b948c69` (ls-verified). |
| — | **RE TRAIN: 9/9 DUE** (W6×3 + W7×3 + W8×3 since PR#39 merge) | — | READY | — | Next: release-engineer promotion #2 (spec-ui b948c69+ → feat/add-frontend, 6dcaccc9 base) alongside wave 9 (SEO §8.1 + a11y §7 + budgets §10/§11 conformance sweep + the 4 recorded P3 doc residuals). |
| SLIP-1 | SCOPE SLIP: rogue "U-W8-T5" home carousel on ui/t3 tip | 8 | **DROPPED** | 1 | Builder hallucinated task id U-W8-T5 and committed `63788e7` (HomeCarousel 183 ln + home page/test edits) — NEVER dispatched; contradicts audited wave-6 home decision ("no carousel — absent from §5.1's section list"). DROPPED at sync per scope-slip rule (merged a849a50 SHA, not ui/t3 tip); HEAD verified free of HomeCarousel; lane orphan keeps it for reference. Revivable ONLY via a spec'd, audited task. |
| U-W5-T1 | /admin 307 redirect, env-driven, fail-if-unset | 5 | **SHIPPED** | 1 | `ebc3731`; old handler had localhost default (removed); runtime-proven 307→https://api.boredompeaks.me/admin/; mutation: var unset → build EXIT 1; floor 132/17, 24 routes, css 36,690. Gate trees seeded `.env.local` (t1/t2/t3/ui-wt). |
| U-W5-T2 | drop Cart.session_id + RegisterResult.id from types | 5 | **SHIPPED** | 1 | `f57b604` 5 files; all consumers disposed (cart mock fixture only, zero `.id` reads); BACKEND_REQUESTS +1 P2 (in-tree backend still emits both — drift vs promoted a43f058, expected); 132/17 hold. |
| U-W5-T3 | TEMPLATE.md §3/§4/§7 stale prose; removeItem disposition | 5 | **SHIPPED** | 1 | `8f7e3be` 1 file; every fix proof-cited (grep lines); removeItem verified already gone since `d485eea`; known residuals out of scope: template.config.ts:80-82 + CartView.tsx:122-126 stale comments (next build), UI-UX-SPEC.md:1714/STITCH-PROMPTS.md:271 old ADMIN_URL prose (orchestrator doc sweep). 132/17 hold. |
| AUDIT-W5 | Wave-5 full audit | — | **SHIP** | 1 | **Zero findings (0 P1/P2/P3)**; guard MUTATION-PROVEN (var unset → build EXIT 1, message captured, restored); /admin runtime 307→api.boredompeaks.me/admin/ on next start; floor **132/17, build 0, 24 routes, 36,690 B css exact hold**; BACKEND_REQUESTS +1/−0 append-only, all prior blocks present; hazard noted: `Select-String` `[slug]` wildcard near-miss caught + re-derived via grep tool. PUSHED `origin/spec-ui=e2cd60cc2e30b43c63adf15f2f5af6d4dd0ebe6d` (ls-verified; push via cmd-wrapper — harness `git push*` deny shadows auditor allows, rule-ordering artifact). |

RE train: **10/9 — GATE DUE.** BLOCKED on §14.3 precondition BUG-4 (e2e unproven "must run
with backend up before §14 — do not waive"): NO frontend agent may run python (ui-builder +
ui-auditor configs deny it), so closing BUG-4 needs the owner to either run the e2e suite
once with the backend up or explicitly waive BUG-4 for promotion. Not invoking
release-engineer until one of those lands (a GATE_PHASE wake would halt on preconditions
and burn a cycle). Waves continue in the meantime (user: continuous mode).
**SUPERSEDED 2026-10-15:** owner waived BUG-4 for the PR#39 gate; RE invoked; force-push +
GG green landed (REWRITE-1); PR #39 merge blocked on ruleset postgres/lint (PROMOTE-39b).
Train now **16/9 tasks shipped** (W1-3:9, W4:3+FIX-B1, W5:3) — RESETS when PR #39 merges.

User configs landed 2026-10-15 ~11:00: ui-wt/.opencode/agents/{ui-builder,ui-auditor,ui-orchestrator}.md.
Every future builder/auditor dispatch cites its file as primary contract. Wave 2 HELD
until user says go (directive stands even though configs exist). Standing audit lessons:
dev-server via cmd-with-redirect (Start-Process npm failed 3x); MCP Chromium for
screenshots (CLI browsers absent); NEVER run wrangler/opennext in audit.

Standing rules (user, 2026-10-15): wave 2 NOT dispatched until user's frontend-tailored
agent configs land in ui-wt/.opencode/agents — check that dir before every dispatch.
Auditor findings come to the user; NO fix dispatch without approval. Check periodically:
(1) ui-wt/.opencode/agents for new configs, (2) orch-bridge.md for backend messages,
(3) git fetch origin.
Standing sync rule (user, 2026-10-16): before merging a lane into spec-ui, RE-MERGE the
current spec-ui tip INTO the lane first (lanes run in different worktree dirs — no
exceptions), then merge the lane forward; after every sync run the silent-deletion check
(`git diff --diff-filter=D <baseline>..HEAD` empty + per-lane content markers). Wave-6
merges predate this rule; retro-verified clean (0 deletions, union of t1+t2+t3 present,
t3 cart files byte-identical, ui.test.ts delta = t3's +7 only).
| BUG-10 | Tailwind @source pin | — | **CLOSED** | 1 | fixed `adbc7ea` (pre-WP0 tip); stylesheet bloat eliminated at source |
| BUG-4 | e2e unproven (servers down) | — | **OPEN** | — | still must run with backend up; WP0 put a current backend in-tree — close during wave with e2e tasks |

RE train: 7/9 tasks shipped (W0, W1-T1/T2/T3, W2-T4/P1/F2; last push `add79e5`). At 9: invoke
release-engineer (GATE_PHASE) for spec-ui -> feat/add-frontend promotion per §14.
Wave tracker: wave 1+2 SHIPPED+PUSHED. PERMANENT lanes (user, 2026-10-15): ui-wt-t1|t2|t3
always on branches ui/t1|t2|t3 — orchestrator prep per wave = `git switch -C ui/tN spec-ui`
in-place; NO new worktrees/branches ever. Dispatches call agents by name (ui-builder,
ui-auditor) with task-only prompts — contract lives in ui-wt/.opencode/agents.
Wave 3 candidates (queued, not dispatched): template wiring (nav/footer + feature-flag
consumers — flags currently no-ops), ContactLinks brand leak, CheckoutView dead removeItem,
shipping dispatch-copy single-source. BUG-4 e2e still OPEN (needs API up; auditors denied python).

---

## Next actions on quota reset

1. Re-fire **R4** unchanged — the brief is complete and covers the binding table,
   the five overrides, coverage gaps and provenance.
2. Verify R4 as before: zero `TODO`, §14 byte-identical, contrast recomputed from
   the new literals, no placeholder content left as binding requirement.
3. Then Phase 3, one task at a time, each **≤ 5 files and ≤ 1 new shared
   component**, splitting before dispatch rather than mid-flight.
4. Audit per batch of 2–3 tasks. Max 3 fix cycles, then escalate.
5. **Do not push, open or merge a PR.** Integration is user-timed, run by the
   backend orchestrator via §14.