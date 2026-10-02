# UI/UX specification — perfume-store storefront

> **STATUS: SKELETON.** This file ships the structure, the rules and the worked
> examples. `design-research` fills every `TODO`. An unfilled `TODO` is a
> dispatch blocker — the UI orchestrator will not begin implementation while any
> remain.
>
> **Depth bar: `backend/docs/conventions.md`.** That file is the standard. A
> section here that is hand-waving where `conventions.md` is specific is not done.
>
> Owner: `design-research`. Consumer: `frontend-architect`.

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
a **redesign, not a rewrite from nothing**. `design-research` must read the
existing `frontend/src` tree fully and record, per existing asset:

- **KEEP** — what survives unchanged, and why
- **CHANGE** — what is replaced, with the before and after
- **DROP** — what is deleted, and what depended on it

Anything already passing WCAG AA that the redesign would break is a **regression**,
not a trade-off. The recorded status is: palette and computed text-pair contrast
pass AA, an interactive-state matrix exists, a focus trap exists, a skip link
exists, and `prefers-reduced-motion` is handled. Confirm each in source before
claiming it.

---

## 1. Design principles

> **TODO — `design-research`.** Include the decision test: *does this animation
> communicate state or direct attention?* If neither, it is cut. Three to five
> principles, each with a concrete example from the reference sites.

---

## 2. Tokens

The single source of truth. Everything else cites these names.

### 2.1 Colour

Every text/background pair that ships states its **computed contrast ratio** and
its target (AA normal text 4.5:1, AA large text 3:1, non-text/UI 3:1).

| Token | Value | Contrast on | Ratio | Target | Meets |
|---|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO | TODO |

### 2.2 Typography

The pairing, the scale, line-height rules, and the loading strategy.

> **TODO — `design-research`.** Must specify self-hosted via `next/font`, subset,
> no render-blocking third-party font request, and a `font-display` choice with
> its layout-shift consequence.

### 2.3 Spacing, radii, borders, elevation, z-index

> **TODO — `design-research`.** A scale, not ad-hoc values. State the rule that
> makes a value land on the scale rather than near it.

### 2.4 Motion tokens

Durations and easings **by intent class**, not by component — so the whole site
feels like one system.

| Intent | Duration | Easing | Reduced-motion behaviour |
|---|---|---|---|
| TODO | TODO | TODO | TODO |

---

## 3. Motion

The load-bearing table. The "why" column is not commentary — it is the
acceptance criterion.

| Element | Trigger | Technique | Duration | Easing | Reduced-motion | Why it earns its place |
|---|---|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO | TODO | TODO |

Plus:

- **Scroll-driven work** — what uses `animation-timeline`, and the amplitude cap.
- **Route transitions** — whether any exist, and why.
- **The reduced-motion policy** — enforced at the token layer, so a component
  cannot opt out by forgetting.

---

## 4. Responsive

A breakpoint table with **concrete per-breakpoint layout differences** — the word
"responsive" is not an answer.

| Breakpoint | Range | Nav | Grid | Type scale | Page-specific changes |
|---|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO | TODO |

---

## 5. Per-page layout

One subsection per row of the `STITCH-PROMPTS.md` §2 coverage contract. Each:
sections in order, composition, imagery, motion, and the routes that share it.

> **TODO — `design-research`.** Include an explicit out-of-scope decision for
> `/admin` and `/staff/*` with the reason (operational tools, not brand surfaces).

---

## 6. Component inventory

Every reusable component with its **loading / empty / error / skeleton** states.
The empty and error states are where most redesigns quietly fail.

| Component | Used on | Loading | Empty | Error | Skeleton | Notes |
|---|---|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO | TODO | TODO |

---

## 7. Accessibility

The non-negotiable floor. A design that cannot meet this is rejected however
beautiful.

- Keyboard order and focus trapping (drawers, modals, the gallery)
- Focus visibility — must be visible on every interactive surface
- Contrast targets (from §2.1)
- `prefers-reduced-motion` (from §3)
- The skip link — keep or replace, but it must exist
- Any regression against the current implementation's passing AA

> **TODO — `design-research`.**

---

## 8. SEO

Per-page metadata strategy, canonical URLs, Open Graph, JSON-LD types, sitemap
strategy, robots.

**And the specific question this redesign creates:** how does scroll-driven
animation coexist with crawling? State the answer concretely — what content is in
the static HTML, what is progressive enhancement, and what a crawler with
animations disabled sees.

> **TODO — `design-research`.**

---

## 9. Testing plan

Which flows get Playwright e2e coverage (only flows with real logic or risk — cart
math, coupon apply/remove, checkout through to the payment handoff, form
validation), which utilities and hooks get Vitest unit tests, and explicitly
**what gets no test and why** (purely presentational components).

> **TODO — `design-research`.**

---

## 10. Performance budget

Targets, per-route JS budget, image format and sizing strategy, font loading, and
what is deferred until interaction (e.g. the payment script loads at the payment
step, never globally).

> **TODO — `design-research`.** State the Lighthouse targets the site will be
> held to and the per-route JS budget that enforces them.

---

## 11. Dependency budget

Every proposed package. The current tree has **no animation library**.

| Package | KB (gz) | Route-scoped? | Why CSS cannot do it | Alternative if dropped |
|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO |

**Total: TODO.** If the total is unacceptable, say so here and give the cheaper
alternative — do not hide it in a footnote. "Animation-heavy but zero added
dependencies" is a legitimate and often correct outcome for this stack.

---

## 12. Backend gaps

Pages needing something the backend does not provide. Record only — do **not**
work around it in the client, and do **not** implement a second copy of business
logic. Money, stock, discount, coupon and order state are server-side and
authoritative.

| Priority | Page | What is missing | UX it blocks | Proposed contract | Current workaround |
|---|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO | TODO |

---

## 13. Assumptions

Every claim in this spec that could not be verified from source, listed. An
unstated guess is a defect in this deliverable.

> **TODO — `design-research`.**

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
