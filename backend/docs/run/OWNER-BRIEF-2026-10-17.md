# Owner brief — 2026-10-17: status, blockers, proof-program detail

Written by the orchestrator at the owner's request, on the eve of GATE-4
(promotion + GHCR images). Every number here is either auditor/measurement-
sourced or cited from the named file — nothing hand-typed from memory.

---

## 1. The gate you ordered — DONE (GATE-4 MERGED, images on GHCR, box already redeployed)

- **PR #43 MERGED @ `270fe7d`** (gh-verified 2026-10-17T10:23:35Z,
  `spec-comp -> feat/add-frontend`, all checks green, GitGuardian PASS).
  Integrated floor: **2173 OK both engines**, cov 99.81%, xf=2 (RE-measured).
- **GHCR images:** `ghcr.io/boredompeaks/perfume-store-backend-backend`
  — immutable `:sha-270fe7dc4ee772c9a0c2fc7b6769321b53a8f48c`
  (digest `31ad96de…`), floats `:feat-add-frontend` (release line) and
  `:spec-comp` (work line). Pull/rollback = any older `:sha-<full-sha>`.
- **No manual redeploy was needed:** the deploy gate FIRED on the merge
  (run 39024965172) — `release-uat OK`, container Healthy, `/health/` green.
  **The UAT box now runs `270fe7d`: the CORS fix is LIVE.** A fresh preflight
  should now answer `Access-Control-Allow-Headers` with `idempotency-key` —
  try checkout again. Deploy-gate bonus: RE's `3c7d185` checkout-retry fixed
  the collision that had broken release run 38989889212, and that fix proved
  itself live in this very run.
- Lane merges by orchestrator (your interim authorization): `7d54b00` +
  `11a79fd` (changes.md row-concat, union asserted, markers=0/CR=0/LF).

---

## 2. BLOCKERS — everything waiting on you (the list)

| # | Blocker | What it unblocks / why it matters |
|---|---|---|
| B1 | **UAT env ops**: run `manage.py seed_demo` on the box; set `RAZORPAY_WEBHOOK_SECRET`; shipping enabled (env/DB config) | The **34 BLOCKED red-team rows** + **T1a-second-pass** + clean state for **T2-exec**. (Partially done already: your "Oud" product + `test` account exist.) |
| B2 | **`REDIS_URL` on the box** | Kills finding **F3-C4** (per-process throttle cache × gunicorn workers inflates effective budgets ~2–3×; refusal holds per client but windows drift). Same env line fixes the auth/register/recovery inflation family. |
| B3 | **k6 binary absent everywhere** (measured `Get-Command k6` -> not found) | **T2-exec** cannot start. Profiles are authored + syntax-validated only. Install on the box (preferred — same region) or wherever you want the generator. |
| B4 | **0-byte cleaner still active** (3 strikes: 22 tracked init files deleted mid-merge; venv `contenttypes` wiped twice incl. mid-audit) | External process deleting 0-byte files. You must find/stop it. Repairs proven: tracked files `git checkout --`; venv `pip install --force-reinstall --ignore-installed --no-deps -r requirements.lock`. |
| B5 | **Q-G1 guest-mint decision** (bridge BE-011) | FO's top blocker (spec 1.13 guest payment gap — guest can order but not pay). Held for your ruling. |
| B6 | **FO serve restart** (`start-all.ps1`) | FO compact pending (context 663k before serve went down). |
| B7 | **Permanent `git merge` allowance for release-engineer** | Until then the orchestrator keeps doing lane merges as your interim delegate (GATE-2/3/4 precedent, recorded each gate). |
| B8 | **Line-ending attributes file** | Measured hazard: `core.autocrlf=true` made 84/417 files differ between worktrees; a floor was a property of a checkout, not a commit. Real fix = `.gitattributes` — owner decision (changes every checkout in the repo). |
| B9 | **GitGuardian dismissals** | App-check reds on ANY historical occurrence in a PR range even after fixes (twice: incidents 38269594/95, 38291408 — you dismissed). Expect it again when history touches a prior-triggered path; your dashboard dismissal is the only clear. |
| B10 | ~~CORS fix reaches the live box only via your redeploy~~ **RESOLVED — auto-deploy fired; box runs `270fe7d`, CORS LIVE** | Was: image built 2026-10-16 predates `d5a0e20`. The GATE-4 merge triggered the deploy gate; `/health/` green on the new build. Re-test checkout in the browser — if it still fails, capture the preflight again (it must now list `idempotency-key`). |
| B11 | **Q-HERM1 (hermetic tests) awaiting your go** | Your directive: live URL only for finding/fixing bugs, never permanent test creds; fixture creds hygiene. Frontend half routes to FO via the bridge. |
| B12 | **`X-Guest-Order-Token` + CORS fixed in code** (same fix) | Guest order reads + `/track-order` from the browser were broken by the identical preflight gap; heal with the same redeploy. |

Non-blockers, for calm: `DisallowedHost` log noise (`guptagroup-iitd.com`,
raw-IP hits) = internet scanners being rejected correctly; `/api/health/` 404
is by design (public health lives at root `/health/`).

---

## 3. What this run has accomplished (evidence-backed)

- **3 promotion PRs merged & gh-verified:** PR #38 @ `a43f058`, PR #40 @
  `63e2778`, PR #42 @ `636d06b` (7-task partial train). GATE-4 adds Q-CORS1 +
  T1F2a + T1F2b (3 more tasks; train trigger = 6 tasks or 90 min).
- **Phases shipped:** **S** hardening (Q-S1..S4, Q-P1, Q-RS1 incl CSRF/XSS
  layer, rate-limit tiering), **P** pairs, **D** docs & hygiene (Q-D2a/D2b/D3/
  D4/D6/D7 — Q-D7 froze the deterministic celery-pin flake window
  02:17–03:17 UTC), **T** proof program (below).
- **Floor progression:** 1844 (two-lane anchor) → **2165** tests OK (lane/b),
  lane/a 2162 — both engines, cov **99.81%**, xf flat at 2. Zero regressions
  at every gate.
- **Red team executed against the live box** (T1a+T1b): **101 rows — 65 PASS
  / 2 FAIL / 34 BLOCKED-on-env** + `security_smoke.sh` **52 checks green**.
  - FAIL F7-B1 (non-object JSON → 500): **FIXED** (T1F1 + full sibling sweep
    T1F2a/b, mutation-proven).
  - FAIL F3-C4 (throttle scope drift): **OPEN → B2 (REDIS_URL)**.
  - What held: guest dead-end uniform 403, CSRF/origin matrices, cart
    isolation + fixation, RBAC×IDOR×delist byte-uniformity, envelope
    non-existence oracles, SQLi/mass-assignment, MFA refusals, webhook
    fail-closed (unsigned → 503).
- **CORS root cause found by reading the actual server logs** (box access
  log: OPTIONS ×8 answered 200, zero POSTs ever arrived; your login between
  attempts succeeded — auth was never the issue) → fixed in `d5a0e20`.
- **fe-bridge MCP operational** (FO channel: 10 tools, worker queue,
  notifications; standing rule: talk via the bridge FILE `orch-bridge.md`).
- **Standing rules hardened:** rule 5 (never read/echo key material — route
  via `ssh uat` alias), reshape-over-allow fixtures, lanes never two suites
  on one DB (pg2a/pg2b measured isolation).

---

## 4. What's in the k6 profiles (`scripts/load/k6_profiles.js`, 714 lines)

One file, **three profile families**, selected by `K6_SCENARIO`. Target host
only via `LOAD_BASE_URL` env (no hardcoded host). Browser-shaped UA sent
explicitly (Cloudflare blocks python's default UA — measured in T1a).
Profiles are sized **under the throttle budgets** (auth 10/min, cart 60/min,
coupon 10/min, payment 10/min, per-client): a 429 wall is a *recorded
outcome*, read apart from failures by the knee rubric.

| Family (`K6_SCENARIO`) | Scenarios | Load shape | Thresholds |
|---|---|---|---|
| **browse_cart** | `browse_reads` | 20 RPS constant-arrival × 2m (list→detail→config→cart GET; 30/60 VUs) | p95<500ms, err<1%, checks>0.99 |
| | `browse_cart_ops` | 2 RPS × 2m — session+CSRF add→patch→delete cycles (8/16 VUs) | p95<500ms, err<1% (4xx here = CSRF/throttle contract broke = finding) |
| **checkout_ramp** | `checkout_ramp` | ramping-VUs: 5 → hold 2m → 12 → hold 3m → ramp down; per-VU: login once (vaulted JWT), cart add, checkout, 5s pacing — **internal latency only** | p95{svc:internal}<500ms, err<1%; 429 flagged separately |
| | `intents_capped` | **only** gateway-touching scenario: starts 11m30s, 4 VUs × 5 iters = **hard cap 20 intents** (`LOAD_MAX_INTENTS`; setup() *refuses* to start if env overrides break the math); each iter = fresh checkout + `create_payment`, tagged `svc:razorpay` so gateway vs internal p95 is a threshold away | p95 internal<500ms, p95 razorpay<2500ms, err<1% |
| **race_suite** | `race_oversell_checkout` | **8** distinct buyers (`LOAD_RACE_N`), one simultaneous checkout each, last-unit product | checks==1, p95<500ms, race err<5% |
| | `race_coupon_checkout` | same shape with the `usage_limit=1` coupon, start-staggered +90s (past shared login-throttle window) | same |
| | `race_capture_replay` | **6** parallel verifies (`LOAD_CAPTURE_REPLAYS`, ≤ payment 10/min budget) replaying ONE pre-minted (order,payment,signature) triple, start +180s | same |

Cross-cutting: distinct buyers per VU (same-owner pending-order dedup can't
collapse racers); product slugs/coupon/capture-triple all env-supplied;
capture triple minted by `run_races.py mint-signature` **server-side so the
Razorpay secret never appears on a command line**; every scenario carries the
knee pair (p95 + error ceiling) — auditor BUG-2.

---

## 5. Race conditions tested

**Authoritative runner: `scripts/load/run_races.py`** (1118 lines, stdlib
urllib + threads, Celery-free, no k6 dependency — k6's race legs are HTTP
mirrors that print the same T2-INV markers):

1. **Oversell** — N distinct buyers each hold a cart line for the last unit;
   all fire checkout simultaneously; **all winners mint an intent; then all
   captures fire simultaneously — exactly one capture may win.**
2. **Coupon double-burn** — same shape, every checkout carries the
   `usage_limit=1` coupon; the single-use burn must land exactly once under
   concurrency (checkout+capture wave).
3. **Double-capture replay** — one order, one minted intent, **M parallel
   verifies of the SAME signature triple**; capture must move stock/money
   exactly once. Signature minted locally: HMAC-SHA256 over
   `<razorpay_order_id>|<payment_id>` with `RAZORPAY_KEY_SECRET` — same
   computation the SDK does, no gateway replay API used.

**Two honest DB modes** (the runner *refuses* to claim invariants without
one): **A. live-DB** (`LOAD_PG_DSN`/`DATABASE_URL`, psycopg already in venv —
connects after the waves) or **B. server-side round-trip** (`--emit-sql`
writes parameterized SQL + expectations; operator runs them on the box;
`--verify-json` evaluates — nothing but row data crosses the airgap). Exit
codes: 0 = waves complete AND every invariant holds (or explicit `--skip-db`
= stamped **UNVERIFIED**); 1 = invariant violated; 2 = preflight error (nothing
raced); 3 = ran but no DB mode to evaluate.

Every race mirrors a **T1a BLOCKED row** — i.e. these are precisely the
attacks the red team could not stage on an unconfigured box.

---

## 6. What contentions are deliberately exercised

- **Stock row contention:** N buyers → insufficient-quantity re-check under
  lock (the hard-oversell impossibility, at real concurrency).
- **Payment capture contention:** M parallel verifies →
  `transaction.atomic` + `select_for_update` + transition guard (exactly-once).
- **Coupon row contention:** concurrent `used_count` atomic burn vs
  `usage_limit=1`.
- **Pending-order dedup guard:** distinct buyers forced so same-owner
  collapse can't fake a result (setup() enforces buyer ≥ ramp VUs).
- **Reservation holds:** losers must release (`T2-INV-8`) — hold-release
  contention with expiry.
- **Throttle-cache contention:** per-process cache × gunicorn workers
  (finding F3-C4 — why B2 exists).
- **DB pool vs connection ceiling:** the knee theory under test — workers ×
  concurrency vs PG `max_connections` (capture samples `pg_stat_activity`).
- **Suite-vs-suite:** never two suites on one DB — pg2a/pg2b isolation
  measured (destruction mode proven: one run's teardown drops the other's
  test DB).

---

## 7. What load / how success is decided

- **Numbers:** see the table in §4 (20 RPS browse, 2 RPS cart ops, ramp
  5→12 VUs with hold bands, ≤20 intents total, race waves of 8/8/6 staggered
  0s/90s/180s). All env-tunable (rates, durations, VU caps, thresholds).
- **Knee rubric** (`T2-plan.md`): a step is the knee when **p95 breaches or
  error rate breaches** with the prior step clean; 429 walls read as
  *throttle ceilings*, not box failure; internal vs Razorpay latency split by
  the `svc:` tags.
- **Resource capture** during runs (`resource_capture.sh`): `docker stats`,
  `pg_stat_activity`, `pg_stat_mem` interval samples + manifest +
  `sentry_issues.json`.
- **Deliverable at execution:** "UAT box limits" report — max sustained RPS,
  knee step, first failing signal, failure mode class (edge/app/database/
  memory/throttle wall), resource ceilings, internal-vs-gateway p95, intent
  count/cap, invariant verdicts, sentry spikes, cleanup — template pre-written
  in `T2-plan.md`; runbook text drafted (appended to `deploy-runbook.md` only
  at execution, by design).
- **Cleanup between passes:** `seed_demo --cleanup` (marker rows only).
- **Status:** profiles + runner + capture + playbook are SHIPPED (T2a,
  promoted in PR #42). **Zero live traffic has ever been sent by them** —
  execution is T2-exec, blocked on B1+B3.

---

## 8. Money invariants (the check vocabulary — k6 markers, runner, report)

| ID | Invariant |
|---|---|
| T2-INV-1 | no product row holds negative stock after a race |
| T2-INV-2 | racing orders captured == expected winners (last-unit: min(stock, racers); coupon: 1) |
| T2-INV-3 | each captured racing order: one SALE ledger row per item, delta sum = −units; each loser: zero SALE rows |
| T2-INV-5 | `usage_limit=1` coupon: `used_count == 1` and exactly one captured order holds it |
| T2-INV-6 | double-captured order: `payment_status == 'captured'`, expected payment reference, **exactly one** pending→confirmed transition |
| T2-INV-7 | SALE ledger rows for the double-captured order == item count (replays never decrement twice) |
| T2-INV-8 | no racing order still holds an ACTIVE reservation after the wave |

T2-INV-4 is deliberately unallocated (the pre/post stock-delta check folded
into INV-2/INV-3 — every check self-contained on the racing order ids).
**Any FAIL = a finding; all must PASS for T2-exec to ship.**

---

## 9. Known gaps / deferred (not yet true, by name)

- **T2-exec** (never run — B1+B3) and **T1a-second-pass** (34 rows — B1).
- **Q-D1** (orchestrator bookkeeping: slim `spec-run-state.md` to pure index
  + promotions register — the Run-pointers block has grown history-style).
- **Q-D5** final/end-of-run gate; **whole-spec compliance sweep** before it.
- **Q-HERM1** (B11) and the guest-payment **SPEC GAP** (B5/Q-G1).
- **CORS/health truth on the box** — e.g. my probe showed `/api/health/` 404
  (correct: health is root `/health/`), box answers the new Q-S2 body already
  (`shipping_configured:false` until you enable shipping).
- **FO/frontend:** spec-ui at wave 6 (`33b3b50`), PR #39 (their CI sentinel)
  merged; their remaining blocker = Q-G1; integration with `feat/add-frontend`
  is your timed call (RECONCILIATION CHECKLIST in UI-UX-SPEC.md, run verbatim
  in ui-wt when woken for it).

---

## 10. Risk register (things that have actually bitten, or credibly can)

1. **0-byte cleaner** (B4) — 3 strikes; the only recurring infrastructure
   attacker in this run.
2. **CRLF/checkout divergence** (B8) — floors measured per-lane were partly
   checkout properties; `.gitattributes` is your decision.
3. **GitGuardian historical-occurrence reds** (B9) — can't be fixed by
   builders without history rewrites (forbidden); your dismissals clear them.
4. **Celery-pin deterministic flake** — 02:17–03:17 UTC window; Q-D7 froze it,
   avoid scheduling suite runs inside it until the freeze ships to the box.
5. **RE permission wall** (B7) — works via interim; permanent fix is one
   config line from you.
6. **Suite concurrency** — hard rule: never two suites on one engine/DB.
   Lanes parallelize only with per-lane containers.
7. **Prose-drift cycles** — 10 of 13 historical fix cycles were prose, not
   code; doc gates (`doc_claims`, `changelog_figures`) + the DOC-ONLY fast
   path are the defense; run them locally before every push (they also run in
   CI — GATE-7).

---

## 11. How to redeploy (once GHCR images exist — full brief rides the RE report)

Shape: pull `ghcr.io/<owner>/<repo>-backend` (and frontend image if built) →
`docker compose` overlay per `backend/docs/deploy-runbook.md` → set the env
secrets from §2 (B1/B2 first) → restart. The release-engineer returns the
exact pull commands + config table with its MERGED report; the operator
sequence on the box is `scripts/release.sh` (the same script CI's deploy job
runs, by design — rehearsed == real).
