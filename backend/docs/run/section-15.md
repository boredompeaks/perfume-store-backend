# Section 15 — UI/UX design system specification (task ledger)

Created 2026-09-19 from the S15 compliance prefetch. Spec lines: 3855–4088. Prefetch result: 60 rows — 36 IMPLEMENTED / 8 PARTIAL / 5 MISSING / 11 N-A (verified) / 0 DEVIATES.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-15-1 | 15 | P2 — WCAG AA failures [R-15.4.1/R-15.4.7]: (a) placeholder text ≈3.6:1 < 4.5:1 (placeholder:text-ink-muted/70); (b) input borders border-line #e6e1d7 ≈1.2-1.3:1 < 3:1 WCAG 1.4.11. Fix: drop the /70 modifier, dedicated field-border token, centralize the duplicated inputClass into one shared field class | 3855–4088 [R-15.4.1],[R-15.4.7] | SHIPPED (audit cycle-1 SHIP @ f13b4c5: independent WCAG recompute matched to 4dp — placeholder 7.6293 surface / 7.1882 paper, border 4.9041 / 4.6205, OLD failures reproduced as sanity; vitest 24/24 + build clean strict-TS re-run by auditor; line-strong proven in compiled .next CSS, `line` byte-unchanged, 0 residual ink-muted/70, 0 duplicate inputClass copies; 3 extra-scope files verified inputClass-swap-only; BUG-1 P3 non-blocking prose nit: build route table has 23 entries, not 21 — corrected here, changelog itself was truthful; pushed aa01edf..b9f153d; auditor commit denied by env rules — SHIP row written by orchestrator, tree was pristine) | 1 |
| SPEC-15-2 | 15 | P3 — CartLineItem.tsx:116 quantity span has NO aria-live (AddToCartButton.tsx:71 has it) — line-level qty changes are silent to screen readers; add aria-live polite for parity | 3855–4088 [R-15.4.11] | PENDING (P3) | 0 |
| SPEC-15-3 | 15 | P3 — Checkout stepper flow (details→review→pay) has no visual step indicator/progress affordance (PLAN.md:124 already promised it); add to CheckoutView.tsx | 3855–4088 [R-15.2.18] | PENDING (P3; note SPEC-3-03 owns the server-side terms-acknowledgement half of checkout UX) | 0 |

Owner attributions (no duplicate tasks):
- Missing primitives with no current consumer (Checkbox, Radio group, Date picker, Confirmation dialog) + extraction of inline Alert/EmptyState/ErrorState + shared inputClass centralization + tokens.ts naming pointer (it is the AUTH token module, not design tokens) → **SPEC-2-02**.
- Semantic Success/Error color tokens in @theme → **S16** (theme config owner; current bronze-error passes contrast).
- Mini-cart/cart drawer (deliberate omission, PLAN.md:104) → **SPEC-3-11**. Mobile sticky add-to-cart bar (PLAN.md drift) + gallery/variants if backend grows them → **SPEC-3-04**. Mega menu N-A (no categories endpoint — PLAN.md:31).

Grandfathered preference deviations (NOT task-worthy): palette hexes differ from illustrative spec palette (spec line 3863 delegates the choice; all computed text pairs pass AA), errors styled bronze not red, Fraunces/Source Sans fonts, full-screen mobile overlay vs off-canvas drawer, cart page vs slide-in drawer.

Verified strong (highlights): interactive-state matrix complete across components; MenuOverlay focus trap + Escape + focus restore; skip link; global :focus-visible 2px bronze ring (5.6:1); labelled forms + role=alert/aria-describedby pattern everywhere; global prefers-reduced-motion kill-switch; all text pairs computed ≥ AA (ink/paper 16.5:1, muted 7.2:1, bronze 5.6:1). Test-coverage note: no component/a11y tests (documented decision PLAN.md:186); an axe-core Playwright pass would close the WCAG regression gap cheaply — candidate ride-along for SPEC-15-1.
