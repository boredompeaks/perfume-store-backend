# Decision record — product, governance, security, ops

Owner decisions and orchestrator-applied repo state. Append-only in spirit:
new decisions get new dated entries; never silently rewrite an old one.

---

## 2026-10-15 — GitHub repository protections APPLIED (orchestrator, owner GO)

Four repository rulesets created via API, all `enforcement: active`, verified live:

| Ruleset | ID | Ref | Rules |
|---|---|---|---|
| `master-freeze` | 25129525 | `refs/heads/master` | deletion blocked, force-push blocked, **PR required** (0 approvals — solo-safe) |
| `release-branch-gate` | 25129526 | `refs/heads/feat/add-frontend` | deletion + force-push blocked, **required status checks: `postgres`, `lint`, `gitleaks`** on every PR (non-strict). Structurally closes the PR-#34 "merged over red checks" failure mode |
| `integration-branch` | 25129543 | `refs/heads/spec-comp` | deletion + force-push blocked |
| `lane-branches` | 25129544 | `refs/heads/lane/*` | deletion blocked only (lanes permanent; ff/rebase still allowed) |

Deliberately NOT a required check: **GitGuardian** — owner dismissed incident
37763436 (compose `:?` guard template, zero literal secrets) and a future FP
must never hard-block merges. gitleaks (repo-owned engine) is the required
secret gate. Decision: keep GitGuardian informational unless FP rate proves
otherwise.

Deliberately NOT a `pull_request` rule on `feat/add-frontend`: owner retains
direct-push for their own frontend/config commits; agent/RE promotions already
go through PRs where the required checks bind.

Topics applied (name-neutral): `headless-commerce`, `e-commerce`,
`self-hosted`, `django`, `django-rest-framework`, `nextjs`, `postgresql`,
`docker`. Repo **description held** until the product name is chosen (it is
the main naming surface).

---

## 2026-10-15 — Product positioning (owner)

Not "a perfume store": an **opinionated, distilled enterprise-commerce
platform** for smaller merchants who do not need enterprise weight — the
essential ~20% (catalog, cart, checkout, payments, orders, scoped admin,
audit trail, headless API) without promotions engines / multi-warehouse / B2B
/ complex tax. White-label: merchants never see the platform name. Sold and
implemented via retainer. Storefront brand in demos is "Maison Aurel"
(separate from the platform name).

## 2026-10-15 — Naming (owner)

- Vitrine: shortlisted then **REJECTED** by owner ("not so sure" -> "give
  more options"). A fresh batch was provided inline in chat (Kern, Shophand,
  Shingle, Drygoods, Pennyweight, Trestle, Curio, Cachet + criteria).
- **Repo rename + full migration: APPROVED IN PRINCIPLE**, blocked only on
  the final name. Migration checklist (to execute at one deliberate gate
  after the name lands):
  1. `gh repo rename` (GitHub keeps clone-URL redirects; verify).
  2. **GHCR image path does NOT necessarily follow repo renames** — verify
     `ghcr.io/boredompeaks/<old>` pull continuity after rename; update
     `docker-compose.yml` / `Dockerfile` / README image references; consider
     publishing under the new name with an alias period.
  3. Update README badges, docs references, CI `github.repository` consumers
     (most self-resolve; grep for hard-coded repo name).
  4. Update deploy host checkouts' `origin` URLs.
  5. Product-name sweep: README, description, topics, decisions file.
- Avoid Saleor-adjacent phonetics (Sellora, Salesia, ...) for trademark
  safety — the product is "distilled like Saleor", it must not sound like it.

## 2026-10-15 — License + visibility strategy (owner direction, options pending)

- Owner plan: **repo stays as-is for now; consider private until first
  customers, then public**; license decided later (AGPL or Apache named as
  candidates; more options provided inline: MIT, MPL-2.0, BUSL-1.1 /
  source-available, dual-licensing).
- **License decision is deferred** and crystallizes at (or just before) the
  public moment. While private, client contracts govern use; the license
  governs the public world.
- PLATFORM CONSEQUENCES OF FLIPPING PRIVATE ON THE GITHUB FREE PLAN (decide
  BEFORE flipping):
  1. GitHub-hosted Actions minutes become **metered** (public repos get
     unlimited free minutes; private do not) — CI costs real money. This is
     exactly when the deferred "split/self-host runners" item re-enters scope.
  2. Rulesets/branch-protection availability changes on Free for private
     repos (may require Pro/Team to keep the governance just applied).
  3. Free plan private repos: 3-collaborator seat limit.
  - Mitigation options: upgrade to Pro/Team (~$4-7/user/mo), or self-hosted
    runner, or accept reduced governance while private.
- Recommendation recorded (orchestrator): private-now is strategically fine
  for IP during the build-out, but pair it with public *trust artifacts* that
  don't require the code: product docs site, security whitepaper, and — the
  strongest one — **publish the red-team/load-test results** (Phase T) as
  marketing. "Open-source-based" claims must wait for the public+licensed
  moment.

## 2026-10-15 — Admin + MFA placement (owner GO on Access-in-front)

- **KEEP current hosts.** `api.boredompeaks.me/admin/*` stays on the API host
  (architecturally correct); storefront staff flows stay on the storefront.
  No new hostname, no tunnel-only topology (obscurity already leaks via F4/F5;
  moving changes the label, not the control).
- **GO: Cloudflare Access in front of `/admin/*`** (email OTP / IdP at the
  edge before Django login). Optionally the same Access policy path-scoped to
  `test.boredompeaks.me/staff/*` (path-scoped so customers are untouched).
  Owner configures in the Cloudflare dashboard (agents cannot).
- Verify MFAAdminSite actually enforces the MFA code field on the Django
  admin path (SPEC-17-05 / SPEC-20-13 built it; the pentest only saw the
  pre-enrollment page). If enforced: edge login + TOTP = two factors.
- MFA enrollment stays on the storefront (must be inside an authenticated
  staff session; storefront-staff roles don't use Django admin). Verify
  enrollment requires an already-privileged session + is throttled.
- Ride-along backend task: throttle/lockout on the Django admin login form
  (API auth-scope throttle may not cover the form path — verify).
- Optional garnish later (behind Access, never instead): secret admin path,
  IP allowlist.

## 2026-10-15 — F1-F8 pentest dispositions (owner ratified plan)

| # | Disposition |
|---|---|
| F1 session_id in cart payload | FIX (lane A): drop from `CartSerializer`, repin the lying V-09 test against the REAL serializer, frontend type cleanup. Verified in source: `cart/serializers.py:30` includes the field; `test_v09_cart_payload_excludes_session_id` passes only because it asserts a different payload |
| F2 /health/ business metrics | FIX (lane A): split public liveness (keep `status`+`checks.database` — release.sh wait contract) vs staff-only detail (or delete counters if nothing reads them). razorpay_mode/smtp_configured leave the public body |
| F3 enumeration | FIX (lane B): **ACCESS GATE** (Cloudflare Turnstile/challenge or tightened throttle+challenge) on `username-available` + `register` — owner chose gate instead of endpoint removal / generic-error restructure. Still drop numeric `user.id` from the register response |
| F4 /admin -> 127.0.0.1 | FIX (frontend): env-driven redirect to the real API admin, NO localhost default (prod build fails if unset) — or delete the route. GATED on the open frontend-edit question below |
| F5 sitemap/og localhost:3000 | FIX (frontend): find the var each consumer reads (deploy sets NEXT_PUBLIC_SITE_URL; something else is winning), set in EVERY build path incl. local wrangler. GATED on frontend-edit question |
| F6 storefront HSTS | OWNER DASHBOARD, ~1 min: Cloudflare Always-HTTPS + `Strict-Transport-Security: max-age=31536000` on test.boredompeaks.me; preload later |
| F7 refresh in localStorage | FIX (pair): API sets HttpOnly+Secure+SameSite refresh cookie + Origin check; frontend dual-accept migration window then purge. Backend half lane A; frontend half gated on frontend-edit question |
| F8 admin exposed | Handled by the Access-in-front decision above + admin-login throttle ride-along |

FRONTEND-EDIT QUESTION — **RESOLVED 2026-10-15 (owner): agents handle ALL
frontend fixes; the owner will not touch frontend code.** `frontend/**` is
now in-pipeline scope for the queued security fixes (Q-F1/F2/F3 and the
F7-frontend half). Historical "frontend is owner-only" standing rule is
superseded for these tasks; the owner keeps any creative/product frontend
work to themselves when they choose to do it.
Also recorded: owner has GitHub **Pro** already (private-flip governance
concern moot); mutation-weekly workflow tuned by owner (~6x4 + other tests,
est. <2k units/mo); **not flipping the repo private at this time**.

## 2026-10-15 — Testing program policy (owner override of hybrid proposal)

- **Load / stress / concurrency / resource tests run against the REAL
  URLs** (`api.boredompeaks.me`, `test.boredompeaks.me`). Owner explicitly
  accepts OOM: "idm if prod ooms we need to know its limits; it's not yet a
  client demonstration". The point of the load phase is **finding the box's
  limits** (knee point: gunicorn workers x PG pool vs max_connections; VPS
  RAM ceiling).
- Caveats recorded (not blockers): sustained payment-step load measures
  Razorpay test-API latency, not store capacity — the k6 profile caps intent
  creation and the resource report separates internal vs gateway time; Sentry
  noise during runs is expected; run windows still off-peak by preference.
- Seed data first: promote the demo seed script (untracked until T0 lands it
  in-tree) to a real management
  command (`seed_demo`, marked rows + cleanup) so runs start from known state.

## 2026-10-15 — Red-team scope (owner)

The red team is **not** a re-run of F1-F8. It is an adversarial program whose
explicit charter is: *break every hardening this build run shipped* — the
order-lifecycle hardening (idempotent capture, summed stock re-check, ledger
rows, reservation holds, webhook reconciliation/signatures), coupon
idempotency + over-limit audit, guest-checkout refusal, cart/session model,
RBAC matrix, error envelope, throttles, notifications outbox, media upload
validation — plus regression checks for F1-F8. Full attack catalog lives in
`backend/docs/run/BUILD-QUEUE.md` Phase T.

## 2026-10-15 — Queue approvals (owner)

- Phased build queue: **APPROVED ("lgtm")** with the load-target override
  above. Written to `backend/docs/run/BUILD-QUEUE.md`.
- Docs plan: (a) slim spec-run-state, (c) freshness pass, (b) changes.md
  rotation at the next promotion boundary, (d) opportunistic comment trim +
  conventions rule: **APPROVED**.
- Runners work (splitting/self-hosting): **DEFERRED** — revisit when (1) repo
  goes private (Actions minutes become metered) or (2) PR checks exceed
  ~8-10 min wall-clock. Public-repo GitHub-hosted runners are free; today's
  checks are ~3-4 min.
- SECURITY.md + CONTRIBUTING.md: **QUEUED** (do not require a license).
