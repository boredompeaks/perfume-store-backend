# Build queue — post Tier-1 hardening, security & proof program

Owner-approved 2026-10-15 ("lgtm" with the real-URL load-test override).
Companion decision record: `backend/docs/decisions.md`. Two-lane discipline
continues (lane/a = spec-comp-wt, lane/b = wt-async); every task ships
builder -> auditor -> gate. Frontend-touching tasks (marked FRONTEND-GATED)
are blocked until the owner answers the frontend-edit question in the
decision record.

Status legend: `PENDING` | `BUILDING` | `IN-AUDIT` | `SHIPPED` | `BLOCKED(owner)`.

---

## Phase S — Security fixes (backend, 2 lanes, start immediately)

| ID | Task | Lane | Notes |
|---|---|---|---|
| Q-S1 | F1: remove `session_id` from CartSerializer; repin `test_v09_cart_payload_excludes_session_id` against the REAL payload (it currently passes against a different payload = lying test); frontend `types.ts` field removal rides the frontend phase | A | Verified: `cart/serializers.py:30` includes the field. Regression test: unauthenticated GET /api/cart/ contains neither `session_id` nor the raw session key |
| Q-S2 | F2: split /health/ — public keeps `{"status":"ok","checks":{...deploy-relevant}}` (FIRST verify release.sh's exact wait contract); pending_orders/carts/stock/razorpay_mode/smtp_configured move behind a staff-capability endpoint or die if nothing reads them | A | P2. Deploy pipeline must stay green through the change |
| Q-S3 | F3: ACCESS GATE on `/api/accounts/username-available/` + `/api/accounts/register/` (Cloudflare Turnstile/challenge or equivalent edge gate + tightened throttle); drop numeric `user.id` from the register response. Owner chose gate-over-removal | B | Edge gate may be owner-configured (Turnstile keys); backend half = register response + any app-level challenge hook. Enumerate residual oracles and record them |
| Q-S4 | F8-verify + hardening: prove MFAAdminSite enforces the MFA code field on the Django admin path (SPEC-17-05/20-13 built it; pentest saw only pre-enrollment login); verify storefront MFA enrollment requires an already-privileged session + throttle; add throttle/lockout on the Django admin login form if the API auth-scope does not cover it | B | Test-only unless a real gap is found (then P2 fix in-task) |

Owner-side parallel actions (dashboard, not agents):
- F6: Cloudflare Always-HTTPS + HSTS header on test.boredompeaks.me.
- Access policy on `api.boredompeaks.me/admin/*` (+ optional path-scoped
  `test.boredompeaks.me/staff/*`).
- Turnstile keys for Q-S3 once the backend hook shape is known.

## Phase F — Frontend trio (FRONTEND-GATED on owner answer)

| ID | Task | Notes |
|---|---|---|
| Q-F1 | F4: `/admin` redirect -> `https://api.boredompeaks.me/admin/`, env-driven, NO localhost default (prod build fails if unset) — or delete the route | 127.0.0.1 must never appear in a prod build |
| Q-F2 | F5: sitemap/og/JSON-LD read the right SITE var in EVERY build path (deploy.yml sets NEXT_PUBLIC_SITE_URL already — something else wins); rebuild+deploy; regression: sitemap `<loc>` hosts = public host only | |
| Q-F3 | F7-frontend half: consume the HttpOnly refresh cookie (dual-accept window vs legacy localStorage token, then purge `aurel.refresh`) | pairs with Q-P1 |

## Phase P — Hardening pairs

| ID | Task | Lane | Notes |
|---|---|---|---|
| Q-P1 | F7-backend half: refresh token -> HttpOnly+Secure+SameSite cookie on `/api/accounts/refresh` (+ Origin/CSRF check on the refresh endpoint); dual-accept migration window; then stop issuing body refresh tokens | A | Pairs with Q-F3. Existing logged-in users must not be bricked |
| Q-P2 | (owner) F6 HSTS + Access policies + Turnstile keys — quick dashboard session; checklist in decisions.md | owner | Blocks nothing but Q-S3's edge half |

## Phase T — Proof program: red team, then load against the REAL URL

Owner charter: **"break the hardening."** Not a re-run of F1-F8 — an
adversarial attack catalog against everything this run shipped. All against
the live test URLs (api.boredompeaks.me / test.boredompeaks.me); OOM on the
UAT box is ACCEPTED and informative (finding its limits is the point; not yet
a client demo).

T0 — Seed: promote `scripts/demo_seed.py` -> `manage.py seed_demo` (marked
rows, cleanup flag). Both red team and load runs start from known state.

T1 — Red-team attack catalog (each item = attempted attack + invariant that
must hold + evidence artifact):

1. **Order lifecycle / capture**: replay a captured webhook (idempotency must
   no-op); replay a browser-verify; send both concurrently (double-capture
   race — stock ledger must move once); forge webhook signatures (bad sig,
   alg confusion, truncated sig); amount/currency tampering in verify
   payloads; capture for a non-existent / already-cancelled order; refund
   seam abuse (refund without capture, double refund).
2. **Inventory**: oversell races — N parallel checkouts of the last unit;
   adjust-stock via admin vs concurrent capture (ledger consistency);
   reservation expiry during an in-flight payment; negative-stock attempts
   via every mutation surface (API, admin inline, adjust action); the summed
   re-check (twin-line shape from below the per-line backstop).
3. **Coupons**: double-burn single-use via webhook+verify concurrency;
   over-limit timing (limit reached between cart-add and capture — must be
   recorded-not-prevented + visible); cartless preview oracle; coupon code
   enumeration via error differences; reuse after order cancel.
4. **Guest / cart**: guest-checkout refusal bypass attempts (session
   tricks, keyed replay, authenticated-token + anonymous mix, malformed
   guest_email variants); cart id enumeration / cross-session cart access;
   session fixation via planted cart session; post-F1 regression (session
   key never in any payload); cart clear on capture cannot be tricked into
   clearing another user's cart.
5. **RBAC matrix**: each role x each capability endpoint (inventory role
   attempting publish/delist; catalogue role attempting adjust-stock;
   customer attempting staff surfaces; IDOR on order detail across users and
   guest tokens); delist uniformity (delisted vs nonexistent byte-identical
   on every public surface, including REST search/filters/relations).
6. **Auth / MFA**: credential stuffing window (throttle hold under real
   spray); refresh-token theft scenarios (post-Q-P1: cookie not readable via
   XSS); MFA enrollment abuse (unauthenticated enrollment attempts);
   password-reset oracle re-check; JWT handling (exp, alg, key confusion,
   logout invalidation).
7. **Input / validation**: mass assignment on every serializer (register,
   product, order address); SQLi via search/ordering/filter params (adversarial
   payloads); XSS re-sweep incl. admin surfaces (stored XSS via product
   fields rendered in admin/storefront); file-upload abuse (oversized,
   polyglot images, path-traversal filenames against the S3 seam);
   serialization bombs (deep nesting, huge JSON).
8. **Oracles / info leaks**: error-envelope body/status/timing diffs across
   existence (products, orders, coupons, users); /health/ post-F2 split
   regression; source maps; stack traces under forced 500s; Django debug
   page; CORS preflight abuse (evil origins, null origin, credential
   tricks); Host-header / cache-poisoning probes.
9. **Notifications / outbox**: enqueue flooding via any exposed event
   trigger; dedup-key collision attacks; dead-letter alert abuse; drain
   worker behavior under poisoned payloads.
10. **F1-F8 regression smoke** (the scripted security_smoke.sh): every
    finding's regression test, runnable after each future release.

Deliverable: `scripts/security_smoke.sh <base-url>` (fast regression subset)
+ a red-team report doc (per attack: attempted / invariant / result /
severity) — this report is also a public trust artifact (see decisions.md).

T2 — Load / stress / resource against the REAL URL (after red team):
- k6 profiles: catalog browse + cart ops (read-heavy), then checkout ramp.
- Stress ramp to breakpoint; record the knee (workers x PG pool vs
  max_connections; RAM ceiling; 502/timeout onset).
- Resource capture during runs: `docker stats`, `pg_stat_activity`,
  `pg_stat_mem`, Sentry spike review. Separate internal latency from
  Razorpay test-API latency (cap intent-creation volume in the profile).
- Concurrency/race suite (real parallel HTTP, not in-process): the money
  invariants from T1.2/T1.3 re-run as a repeatable k6/Celery-free script
  with post-run DB invariant checks (ledger consistency, no negative stock,
  exactly-once capture).
- Deliverable: "UAT box limits" report (max sustained RPS, knee, failure
  mode) + runbook section.

## Phase D — Docs & repo hygiene

| ID | Task | Notes |
|---|---|---|
| Q-D1 | (a) slim `spec-run-state.md` -> index template; promotion narratives -> `run/promotions.md` | orchestrator bookkeeping |
| Q-D2 | (c) docs freshness pass, priority: `deploy-runbook.md` (wrong since compose/release changed), `api-contract.md` (error wire format changed by ENVELOPE-WIRE), `test-gaps.md` (0 missed now); mark fix-plan/vulnerabilities/audit as dated snapshots | |
| Q-D3 | SECURITY.md (reporting channel, scope, test-mode note) + CONTRIBUTING.md (lane/spec-comp/PR flow, conventions pointer, test commands) | license-independent |
| Q-D4 | (d) conventions rule ("comment the why and the invariant, never the what; history belongs in changes.md") + opportunistic trim; optional top-10 narrative-header pass | no mass scrub |
| Q-D5 | (b) changes.md rotation -> `changes/changes-2026Q4.md` + pointer row | SCHEDULED AT THIS QUEUE'S FINAL PROMOTION BOUNDARY (byte-integrity baseline + scanner scope updated in the same gate) |

## Phase N — Naming (blocked on owner shortlist reply)

| ID | Task | Notes |
|---|---|---|
| Q-N1 | Availability battery on the owner's shortlist: PyPI, npm, GH org/repo, GHCR path, .com/.io/.dev/.shop/.store domains, quick wordmark search | after owner picks from the inline batch |
| Q-N2 | Rename + full migration per the checklist in decisions.md (incl. GHCR path continuity verification) at one deliberate gate | after Q-N1 + owner final pick |
| Q-N3 | Repo description (held) + README brand sweep | after Q-N2 |

## Deferrals (tracked, not queued)

- Runners split/self-host: revisit on repo-privatization (metered minutes)
  or PR-check wall-clock > 8-10 min. Today: public = free runners, ~3-4 min.
- License: decision at/near the public moment (options inline in chat +
  candidates in decisions.md).
- Client demonstration / production-load as marketing: owner schedules later;
  Phase T data feeds it.
