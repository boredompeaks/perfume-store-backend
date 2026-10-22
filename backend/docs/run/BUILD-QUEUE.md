# Build queue — post Tier-1 hardening, security & proof program

Owner-approved 2026-10-15 ("lgtm" with the real-URL load-test override).
Companion decision record: `backend/docs/decisions.md`. Two-lane discipline
continues (lane/a = spec-comp-wt, lane/b = wt-async); every task ships
builder -> auditor -> gate. Frontend-touching tasks (marked FRONTEND-GATED)
are blocked until the owner answers the frontend-edit question in the
decision record.

Status legend: `PENDING` | `BUILDING` | `IN-AUDIT` | `SHIPPED` | `BLOCKED(owner)`.
Queue execution APPROVED by owner 2026-10-15 (name not yet decided — run
everything EXCEPT Phase N; frontend tasks are agent-owned per owner).
**PAUSED by owner 2026-10-15T11:01** after Q-S1+Q-S3 audits returned SHIP;
resumed 2026-10-15T~17:0x per owner continue. **PHASE S COMPLETE + PROMOTED
2026-10-15** — PR #38 merged `a43f058` (integrated tip 2114 OK both engines,
cov 99.81%, xf=2). Cross-orchestrator coordination channel:
`C:\Users\Admin\AppData\Local\Temp\opencode\orch-bridge.md` (+bridge-watch.ps1).

---

> **PAUSED 2026-10-17 (owner order)** — switched to **MUT-2** (weekly mutation
> results triage) while the owner's webhook secret is days away. Parked, in
> resume order: Q-HERM1 (awaiting go; FE half via FO bridge) -> T1a-second-pass
> (34 rows; needs webhook secret + seed + shipping) -> T2-exec (needs k6 +
> box env) -> Q-D1 (orchestrator) -> Q-D5 (end-gate). Q-G1 stays held.
> Reminder anchored in the index Run pointers (~2026-10-20).

## Phase S — Security fixes (backend, 2 lanes, start immediately)

| ID | Task | Lane | Notes |
|---|---|---|---|
| Q-S1 | F1: remove `session_id` from CartSerializer; repin `test_v09_cart_payload_excludes_session_id` against the REAL payload; frontend `types.ts` field removal rides the frontend phase | A | **SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/a @75b0210; non-vacuity mutation-proven; xf 3->2; mechanism = xfail-masked live leak (brief's different-payload claim wrong, corrected); types.ts drop flagged on orch-bridge BE-003 |
| Q-S2 | F2: split /health/ — public keeps `{"status":"ok","checks":{...deploy-relevant}}` (FIRST verify release.sh's exact wait contract); pending_orders/carts/stock/razorpay_mode/smtp_configured move behind a staff-capability endpoint or die if nothing reads them | A | **SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/a @163851b; floor 2093→2096 both engines, cov 99.81%; waiters proven body-blind; P3 notes (brief figure wrong; fixture email) travel with next build; stale-docs findings routed Q-D2 |
| Q-S3 | F3: ACCESS GATE on `/api/accounts/username-available/` + `/api/accounts/register/` (Turnstile/challenge + tightened throttle); drop numeric `user.id` from the register response. Owner chose gate-over-removal | B | **SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/b @90520c0; dedicated scopes 5/min + env-gated signed-challenge (zero-config invariant verified, mutation 9 pins); tests 2093->2101 (+8); api-contract register row stale -> Q-D2; register `id` drop flagged orch-bridge BE-003; residual oracles run/Q-S3-evidence.md |
| Q-S4 | F8-verify + hardening: prove MFAAdminSite enforces the MFA code field on the Django admin path (SPEC-17-05/20-13 built it; pentest saw only pre-enrollment login); verify storefront MFA enrollment requires an already-privileged session + throttle; add throttle/lockout on the Django admin login form if the API auth-scope does not cover it | B | **SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/b @6a6de0a; floor 2101→2111 both engines, cov 99.81%; AdminLoginThrottle mutation-proven (M1/M2, 3 pins each); matrix run/Q-S4-evidence.md; V-18 in vulnerabilities.md stale (auditor/human file) |
| Q-RS1 | (FO ask BE-012) restock endpoint accepts optional submitted email for unauthenticated guests (throttle-bounded, no enumeration oracle); authed path mails user.email unchanged | B | **SHIPPED 2026-10-15** — cycle 1 BUGS-FOUND (P2: unparseable guest body 400'd instead of uniform 401) + cycle 2 SHIP, pushed origin/lane/b @6ab9305; 2130 OK both engines, cov 99.82%; oracle 12/12; my handoff's authed-400 claim REFUTED (parent=201, adjudicated+verified); shape posted bridge BE-016 |

Owner-side parallel actions (dashboard, not agents):
- F6: Cloudflare Always-HTTPS + HSTS header on test.boredompeaks.me.
- Access policy on `api.boredompeaks.me/admin/*` (+ optional path-scoped
  `test.boredompeaks.me/staff/*`).
- Turnstile keys for Q-S3 once the backend hook shape is known.

## Phase F — Frontend trio (UNBLOCKED 2026-10-15: agents own frontend fixes per owner)

| ID | Task | Notes |
|---|---|---|
| Q-F1 | F4: `/admin` redirect -> `https://api.boredompeaks.me/admin/`, env-driven, NO localhost default (prod build fails if unset) — or delete the route | 127.0.0.1 must never appear in a prod build |
| Q-F2 | F5: sitemap/og/JSON-LD read the right SITE var in EVERY build path (deploy.yml sets NEXT_PUBLIC_SITE_URL already — something else wins); rebuild+deploy; regression: sitemap `<loc>` hosts = public host only | |
| Q-F3 | F7-frontend half: consume the HttpOnly refresh cookie (dual-accept window vs legacy localStorage token, then purge `aurel.refresh`) | pairs with Q-P1 |

Execution note 2026-10-15: npm is denied in the backend-agent sandbox, so
Q-F1 and the Q-F3 frontend half (types.ts drops, cookie consumption,
`aurel.session`/`aurel.refresh` purge) were handed to the frontend
orchestrator via orch-bridge BE-010 with full contracts; backend halves
(Q-P1) ship on the lanes. Q-F2 collapsed to the env-host contract
(FE-003 option b, BE-007).

## Phase P — Hardening pairs

| ID | Task | Lane | Notes |
|---|---|---|---|
| Q-P1 | F7-backend half: refresh token -> HttpOnly+Secure+SameSite cookie on `/api/accounts/refresh` (+ Origin/CSRF check on the refresh endpoint); dual-accept migration window; then stop issuing body refresh tokens | A | **SHIPPED 2026-10-15** — audit cycle 1 FIX (P3 changelog typo) + cycle 2 BUGS-FOUND (P2: 2 tracked empty package-init files deleted in worktree by an external cleaner, restored from HEAD) + cycle 3 SHIP, pushed origin/lane/a @f432791; 2129 OK both engines, cov 99.81%; contract posted bridge BE-015 |
| Q-P2 | (owner) F6 HSTS + Access policies + Turnstile keys — quick dashboard session; checklist in decisions.md | owner | Blocks nothing but Q-S3's edge half |

## Phase T — Proof program: red team, then load against the REAL URL

Owner charter: **"break the hardening."** Not a re-run of F1-F8 — an
adversarial attack catalog against everything this run shipped. All against
the live test URLs (api.boredompeaks.me / test.boredompeaks.me); OOM on the
UAT box is ACCEPTED and informative (finding its limits is the point; not yet
a client demo).

T0 — Seed: promote the demo seed script (untracked until T0 lands it in-tree)
-> `manage.py seed_demo` (marked
rows, cleanup flag). Both red team and load runs start from known state.
**SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/a @be5b53c;
orders app home, marker-queryset cleanup (foreign rows + role groups proven
survive), 2134 OK both engines cov 99.81%; builder also repaired the
external-cleaner venv damage (see Q-D3 report) — owner should stop that cleaner.

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
**T1a (families 1-5) SHIPPED 2026-10-16** — cycle 1 FIX 2xP3 -> cycle 2 SHIP,
pushed origin/lane/a @79da100; 58 live rows vs api.boredompeaks.me (33 PASS /
1 FAIL F3-C4 Medium throttle-cache hypothesis / 24 BLOCKED), smoke 33 checks
green, floors 2151 both engines. **BLOCKED-ROWS OWNER OPS:** seed UAT
(manage.py seed_demo), set RAZORPAY_WEBHOOK_SECRET, one test account,
shipping on — then a second-pass task closes the 24. Q-OPS1 SHIPPED same day
(lane/b @c36a3b5): compose.uat.yml overlay authored, compose config green.

**Q-T1F2 sibling sweep + Q-CORS1 all SHIPPED 2026-10-17, both lanes pushed.**
- **Q-CORS1 (lane/a, `d5a0e20`)** — CORS preflight restore: settings
  `CORS_ALLOW_HEADERS` extends the INSTALLED corsheaders defaults (imported
  constant, not retyped) with `idempotency-key` + `x-guest-order-token`,
  fail-first pinned against the real `IDEMPOTENCY_KEY_HEADER` /
  `GUEST_TOKEN_HEADER` constants + defaults-survival. Root cause proven from
  UAT access logs: OPTIONS x8 answered 200, browser POST never left the client.
- **T1F2a (lane/a, `2fcbe13`)** — uniform-400 non-object guards across
  orders/refund/payment/checkout/coupon/adjust + `tests/test_nonobject_bodies.py`.
- **T1F2b (lane/b, `9e575e6`+`0a62275`, DOC-ONLY c2 `a246245`)** — accounts
  recovery/token guards + `tests/test_nonobject_bodies_accounts.py`; cycle-1
  FIX on a single P3 (missing :235 classification comment) closed by the
  documented fold-in, delta re-audit SHIP.
- Audits: lane/a FULL SHIP 2162 OK both engines cov 99.81% (mutation-proven),
  pushed `74fa867..d5a0e20`; lane/b c2 delta SHIP 2165 OK, pushed
  `5ab76aa..a246245`. Suites never ran concurrently in the first batch
  (sequential by design); the parallel batch used per-lane DB isolation only.
  Non-blocking carried: REDIS_URL owner-op (F3-C4 throttle-cache family).
- **PROMOTED: PR #43 MERGED @ `270fe7d` (GATE-4, 2026-10-17).** Integrated
  pair 2173 OK both engines cov 99.81% (RE-measured); SENT-BACK cycle 1 was
  the OWNER-BRIEF:32 doc-claims parse, closed `2056ea7`; GHCR images
  published and the deploy gate auto-released the UAT box to `270fe7d`
  (CORS live; deploy.yml `3c7d185` checkout-retry proven in the same run).

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
| Q-D2 | (c) docs freshness pass, priority: `deploy-runbook.md` (wrong since compose/release changed), `api-contract.md` (error wire format changed by ENVELOPE-WIRE), `test-gaps.md` (0 missed now); mark fix-plan/vulnerabilities/audit as dated snapshots | Q-D2a **SHIPPED 2026-10-15** (cycle 1 FIX 3xP3 -> cycle 2 SHIP, pushed origin/lane/b @efcbb7a; 2130 OK both engines cov 99.82%); snapshot-marking half = Q-D2b **SHIPPED 2026-10-16** (cycle 1 SHIP, pushed origin/lane/b @15ff0c6; banners + V-18 remediated with cites; 2130 OK both engines) |
| Q-D3 | SECURITY.md (reporting channel, scope, test-mode note) + CONTRIBUTING.md (lane/spec-comp/PR flow, conventions pointer, test commands) | **SHIPPED 2026-10-16** — cycle 1 BUGS-FOUND (P2 cadence-window flake on lane/a accepted pre-existing; P3 ghcr cell) -> cycle 2 SHIP, pushed origin/lane/a @21f78e2; suite 2134 OK both engines cov 99.81%; RE note: ghcr-publish.yml inline comment carries the same false docs-only claim (RE-owned, optional gate chore) |
| Q-D4 | (d) conventions rule ("comment the why and the invariant, never the what; history belongs in changes.md") + opportunistic trim; optional top-10 narrative-header pass | no mass scrub; **SHIPPED 2026-10-16** — cycle 1 SHIP, pushed origin/lane/a @c701460; rule + 4-file bounded trim, AST-equal independently proven, 129 comment lines, 2151 OK both engines cov 99.81% |
| Q-D6 | README refresh (owner 2026-10-15): verify every command still true, freshen product one-liner, keep current repo-name branding — NO product-name rename until name is finalized | **SHIPPED 2026-10-16** — cycle 1 SHIP, pushed origin/lane/b @b6e798a; 4 claim fixes verified (nightly split, deploy paths, engine-assert, one-liner), branding byte-identical, 2151 OK both engines; FINDINGS routed: release-uat.sh references absent compose.uat.yml -> Q-OPS1 (building), frontend/PLAN.md stale status -> FYI to FO |
| Q-D4 | (d) conventions rule ("comment the why and the invariant, never the what; history belongs in changes.md") + opportunistic trim; optional top-10 narrative-header pass | no mass scrub |
| Q-D5 | (b) changes.md rotation -> `changes/changes-2026Q4.md` + pointer row | SCHEDULED AT THIS QUEUE'S FINAL PROMOTION BOUNDARY (byte-integrity baseline + scanner scope updated in the same gate) |
| Q-D6 | README refresh (owner 2026-10-15): verify every command still true, freshen product one-liner, keep current repo-name branding — NO product-name rename until name is finalized | rides Phase D |
| Q-D7 | test-infra: fix two pre-existing environmental reds hit independently by both Q-S1+Q-S3 builders — (a) celery cadence pin is deterministic-fail 02:17-03:17 UTC daily (wall-clock invariant needs a freeze/mock, not the real clock), (b) restore-drill TEMP-glob cross-process race when suites run concurrently | both builders root-caused; **SHIPPED 2026-10-15** — auditor SHIP cycle 1, pushed origin/lane/b @267a68d; window-proof + concurrency-proof + 5 mutations reverted; both engines 2114 OK/99.81% |

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

---

## Batch 2026-10-22 — Tracks A-E + S9/S16 gap close

Owner directive: finish the Track A-E list integrated with the S9/S16
compliance gaps, then **STOP**. No PR merge in this batch; every auditor
pushes its own lane branch for durability.

### Track A-E audit (checked against tree `03322e2`, not assumed)

| ID | Status | Evidence |
|---|---|---|
| SPEC-A-01 | **LEFT (partial)** | `orders/admin.py:258-296` — `address` is in the change-form fieldset (`:211`, `:332`) but absent from `list_display` and `search_fields` (only `city`) |
| SPEC-A-02 | DONE | `orders/models.py:89,151-152` — `secrets`-derived `ORD-YYYY-XXXXXXXX`, never sequential |
| SPEC-A-03 | DONE | `orders/state.py:130-143` enums; serializer exposes `payment_status`/`fulfilment_status`; frontend `OrderStatusBadge.tsx`, `OrderDetailView.tsx` consume them |
| SPEC-B-01 | DONE | `shipping/models.py:630` `ShippingProvider` |
| SPEC-B-02 | DONE | `shipping/admin.py:286` `ShippingProviderAdmin` (RoleAwareModelAdmin) |
| SPEC-B-03 | DONE | `shipping/models.py:370-378` `Shipment.provider` FK + `tracking_url`; `carrier` CharField kept back-compat |
| SPEC-B-04 | DONE | `common/roles.py:79,95` `shipping.manage` (finance/admin), `shipments.write` (support/admin) |
| SPEC-C-01 | DONE | `orders/views/checkout.py:696-709` `quote_shipping(region, postal, method, lock=True)` |
| SPEC-C-02 | DONE | `orders/models.py:46` `Coupon.FREE_SHIPPING`; waiver at `checkout.py:715-728` |
| SPEC-C-03 | **LEFT** | no `Product.free_shipping` anywhere (repo-wide grep) |
| SPEC-C-04 | **LEFT** | product flag gate absent; coupon admin gate to be verified in the same task |
| SPEC-D-01 | DONE | `shipping/lifecycle.py` + `OrderSyncResult`; webhook-driven sync |
| SPEC-D-02 | DONE | `frontend/src/components/orders/ShipmentTracking.tsx` @ `896e84b` |
| SPEC-E-01 | **LEFT** | no `refund-request` route/model; only `admin_order_refund` |
| SPEC-E-02 | **LEFT (partial)** | admin refund exists (`orders/views/refund.py:83`); no customer request to approve |

**Remaining from the bug list: 5** — A-01, C-03, C-04, E-01, E-02.
C-03 + C-04 are one build (the flag and its gate); E-01 + E-02 are two
sequential builds (request entity, then approval wiring).

### Queue (build order, one builder per lane at a time)

| ID | Task | Lane | Status |
|---|---|---|---|
| SPEC-9-T1 | money renders as 2-dp strings on the wire (R-9.3.24) | A | IN-AUDIT `db369cb` |
| SPEC-9-T4 | owner-scoped addresses CRUD (R-9.2.20) | B | IN-AUDIT `80491c4` |
| SPEC-A-01 | surface `address` in admin list + search for `orders.read` roles | A | PENDING |
| SPEC-C-03/04 | `Product.free_shipping` + admin gate + checkout zeroing | A | PENDING |
| SPEC-E-01 | `POST /api/orders/{id}/refund-request/` (owner + captured) | B | PENDING |
| SPEC-E-02 | admin approve/reject the customer request | B | PENDING |
| SPEC-9-T3 | mount inventory adjustment under the admin family (R-9.4.7) | B | PENDING |
| SPEC-9-T2 | `CheckoutSession` + anonymous checkout path (R-9.3.7/12/20) | A | PENDING |
| SPEC-9-T5 | api-contract table covers every write endpoint (R-9.5) | A | PENDING |
| SPEC-16-T4 | `TIME_ZONE`/`LANGUAGE_CODE` env-driven (R-16.4) | B | PENDING |
| SPEC-16-T2/05 | SiteSettings: name/logo/locale + `maintenance_mode` (R-16.3/15) | B | PENDING |
| SPEC-16-T1 | retire stale N-A + prefetch claims (R-16.22) | A | PENDING |

Rules for this batch: one task per lane at a time; no file overlap between
concurrent lanes; every task builder -> auditor -> push that lane; changelog
row per task; no PR opened or merged. Duplicates are merged, not queued twice.
