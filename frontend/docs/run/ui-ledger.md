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
| R4 | **Reconcile spec against the 17 selected Stitch refs** | §2, §3, §4, §7, §10, §12, §13 | **BLOCKED** | 1 | Dispatch failed: `free-models-per-day-stealth` — daily free-model quota exhausted. Brief is written and ready to re-fire. |
| B1 | Phase 3 build | — | **NOT STARTED** | — | Sequenced behind R4. Cannot start: the refs change palette, fonts and radii, so building first means building twice. |

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