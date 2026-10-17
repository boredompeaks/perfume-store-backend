# T2 — Load / stress program: execution playbook (authored at T2-a)

Status: **authored, not executed.** This plan, the k6 profiles
(`scripts/load/k6_profiles.js`), the resource sampler
(`scripts/load/resource_capture.sh`) and the race runner
(`scripts/load/run_races.py`) were all authored with ZERO live HTTP — no
request was sent to the UAT box during T2-a (serial-live rule: lane A held
the live wire). Execution is deliberately a single-command-per-step
sequence below, so the T2 executor spends its context on measurements, not
on design. Target behavior referenced from T1a's red-team run (lane/a tree,
`backend/docs/red-team/T1-report.md`; not synced to lane/b at authoring
time) and from this tree's urlconfs.

## Preconditions (operator checklist — all server-side, values stay env-side)

1. `manage.py seed_demo` has run (marked `demo-`/`DEMO-` rows; the race
   target product exists with a KNOWN stock — the oversell race wants
   stock=1 on `LOAD_PRODUCT_SLUG`).
2. Shipping is configured (`/health/` must report
   `checks.shipping_configured: true` — the race runner preflight refuses
   otherwise, because checkout cannot price a destination without it).
3. `N` distinct storefront CUSTOMER accounts exist for the buyers
   (seed's single `demo-customer` is not enough: the ramp needs at least
   its VU ceiling, the races need `LOAD_RACE_N` — provision via admin or a
   `manage.py shell` one-liner using `validate_password`; privileged seed
   users cannot use the storefront login door). Export them as
   `LOAD_BUYER_LOGINS='[{"username":"...","password":"..."},...]'` in the
   run shell — never in a committed file.
4. A single-use coupon exists (`LOAD_COUPON_CODE`, `usage_limit=1` — the
   seed's `DEMO-ONCE` fits if unused).
5. The box env carries the Razorpay TEST-mode `RAZORPAY_KEY_ID` /
   `RAZORPAY_KEY_SECRET` pair (the capture races mint the gateway HMAC
   locally from that secret; no live gateway verify call is made beyond the
   capped intent mints).
6. No other traffic runs in parallel (serial-live rule + quiet-box
   assumption behind the ledger invariants).
7. `k6` on the runner machine for the profile legs; `psql` + `LOAD_PG_DSN`
   on the box for resource capture and (unless round-tripping) live DB
   invariant checks. No k6 binary was present at authoring, so the profiles
   were validated statically only.

## Environment contract (one shell, exported once)

```
LOAD_BASE_URL=https://api.boredompeaks.me      # the ONLY host reference
LOAD_TRUSTED_ORIGIN=https://test.boredompeaks.me  # if the CSRF allowlist names the storefront
LOAD_USER_AGENT=<browser UA>                   # Cloudflare blocks the default python UA (T1a-measured)
LOAD_PRODUCT_SLUG=<last-unit product slug>
LOAD_COUPON_CODE=<usage_limit=1 code>
LOAD_BUYER_LOGINS=[...]                        # JSON, see preconditions
LOAD_MAX_INTENTS=20                            # hard ceiling on gateway intent mints
LOAD_PG_DSN=<postgres DSN from the box env>    # never typed into any file
RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET          # TEST mode, from the box env
SENTRY_AUTH_TOKEN / SENTRY_ORG / SENTRY_PROJECT  # optional, for the spike pull
```

## Exact execution sequence

```
# 1. start resource capture (blocks; run in its own terminal, Ctrl-C to stop)
bash scripts/load/resource_capture.sh load-runs/t2-$(date -u +%Y%m%dT%H%M%SZ) [duration-s]

# 2. read-heavy profile (catalog browse + cart ops)
K6_SCENARIO=browse_cart  k6 run scripts/load/k6_profiles.js | tee load-runs/browse_cart.log

# 3. checkout ramp + capped intent stage (the only gateway-touching profile)
K6_SCENARIO=checkout_ramp k6 run scripts/load/k6_profiles.js | tee load-runs/checkout_ramp.log

# 4. HTTP-level race legs (optional sugar — the authoritative runner is step 5)
#    double-capture leg needs a pre-minted triple:
#      python scripts/load/run_races.py mint-signature --razorpay-order-id <order> --payment-id <pay>
#    exported as LOAD_CAPTURE_RZP_ORDER_ID / LOAD_CAPTURE_PAYMENT_ID / LOAD_CAPTURE_SIGNATURE
K6_SCENARIO=race_suite k6 run scripts/load/k6_profiles.js | tee load-runs/race_suite.log

# 5. AUTHORITATIVE money-invariant races + post-run DB checks (live-DB mode)
backend\venv\Scripts\python scripts\load\run_races.py run --phase all --results load-runs/races.json
#    round-trip mode instead (no DB creds off-box):
#      ... run --phase all --emit-sql load-runs/inv-{phase}.json --skip-db
#      (run the SQL server-side, collect rows, then)
#      ... verify-json --checks load-runs/inv-<phase>.json --verify-json <rows.json>

# 6. stop capture (Ctrl-C in the capture terminal) — manifest + sentry_issues.json land in the run dir

# 7. cleanup between full passes (marked rows only)
manage.py seed_demo --cleanup && manage.py seed_demo
```

Record per run: the k6 summary end (thresholds + per-tag p95), the race
runner exit code and per-invariant verdicts, the capture `manifest.txt`
path, and the Sentry issue ids that spike inside the window.

## Knee-decision rubric (what the numbers must show)

- **Max sustained RPS**: the highest `LOAD_BROWSE_RPS` at which the
  `browse_cart` pass completes its full duration with `http_req_failed`
  under the configured ceiling, p95 (internal tag) under the configured
  ceiling, and no 502/504 anywhere in the capture window. Raise the rate in
  steps; each step is a fresh pass, not an extrapolation.
- **Knee**: the first step where any of — p95 internal latency more than
  doubles vs the previous step, error rate crosses the ceiling, or 5xx /
  timeout responses appear — while the step below was still clean. The knee
  is a MEASURED step, never an interpolation between two.
- **Failure mode** (name it from the evidence, in this order of reading):
  1. edge: 502/504/CF timeouts in the k6 log with app containers healthy;
  2. app: 500s + gunicorn worker saturation in `docker_stats.csv`
     (CPU pegged, request queueing);
  3. database: `pg_stat_activity_N.csv` shows connections at
     `max_connections`, wait events on Lock/IO, pool/workers × gunicorn
     concurrency exceeding the DB ceiling;
  4. memory: container MemUsage at the box RAM ceiling, OOM-kill rows in
     `docker_stats.csv` / docker events;
  5. throttle wall: mass 429s with everything else healthy — a budget
     finding, NOT a capacity knee (auth 10/min, cart 60/min, coupon 10/min,
     payment 10/min per client; scopes are per-user when authenticated).
- **Resource ceilings** (report as observed, from the capture dir): peak
  container CPU %, peak MemUsage vs box RAM, peak DB connections vs
  `max_connections`, gunicorn worker count × concurrency actually
  configured on the box.
- **Internal vs Razorpay latency split**: only the `intents_capped`
  scenario emits `svc:razorpay` traffic; its p95 is the gateway number, the
  checkout in the same iteration tagged `svc:internal` is the box number.
  Never quote a blended figure. Intent volume is bounded by
  `LOAD_MAX_INTENTS`; if the cap is hit mid-pass, the pass stops honestly
  rather than silently mints more.

## Money-invariant vocabulary (shared: k6 markers, runner, report)

| ID | Invariant |
|---|---|
| T2-INV-1 | no product row holds negative stock after a race |
| T2-INV-2 | racing orders captured == expected winners (last-unit: min(stock, racers); coupon: 1) |
| T2-INV-3 | each captured racing order: one SALE ledger row per item, delta sum = -units; each loser: zero SALE rows |
| T2-INV-5 | usage_limit=1 coupon: `used_count == 1` and exactly one captured order holds it |
| T2-INV-6 | double-captured order: `payment_status == 'captured'`, expected payment reference, exactly one pending->confirmed transition |
| T2-INV-7 | SALE ledger rows for the double-captured order == its item count (replays never decrement twice) |
| T2-INV-8 | no racing order still holds an ACTIVE reservation after the wave |

(IDs are the runner's check ids; T2-INV-4 is deliberately unallocated —
the pre/post stock-delta check folded into INV-2/INV-3 because every check
is self-contained on the racing order ids the HTTP phase learned.)

## "UAT box limits" report TEMPLATE (fill at T2 execution)

```
# UAT box limits — <date, git sha, box>
## Max sustained RPS   — <step> RPS for <profile>, evidence: <k6 log path>
## Knee                — <step> RPS; first failing signal: <p95 | err | 5xx>; prior step clean
## Failure mode        — <edge | app | database | memory | throttle wall>, evidence: <capture dir files>
## Resource ceilings   — peak CPU <%>, peak RAM <used/total>, peak DB conns </max>, workers × concurrency
## Internal vs Razorpay— internal p95 <ms> (svc:internal); gateway p95 <ms> (svc:razorpay); intents minted <n>/<cap>
## Race invariants     — run_races exit <code>; T2-INV-1..8 verdicts (all must PASS; any FAIL = finding)
## Sentry spikes       — <issue ids> in window, or "none"
## Cleanup             — seed_demo --cleanup re-run: yes/no
```

## Runbook TEXT (draft — append to `deploy-runbook.md` at T2 execution; do NOT touch that file before then)

````markdown
## Load & stress testing (Phase T2)

The load program runs from the repo root against the deployed API; the
base URL, buyer accounts, race product and credentials all come from the
environment (see `backend/docs/run/T2-plan.md` for the full contract — no
host, account or secret is hardcoded in any script).

1. Start resource sampling on the box (docker stats + pg snapshots +
   optional Sentry pull; Ctrl-C stops it and writes the manifest):
   `bash scripts/load/resource_capture.sh load-runs/t2-<stamp>`
2. Run the read-heavy profile, then the checkout ramp (the ramp profile
   contains the only gateway-touching stage, hard-capped by
   `LOAD_MAX_INTENTS`):
   `K6_SCENARIO=browse_cart  k6 run scripts/load/k6_profiles.js`
   `K6_SCENARIO=checkout_ramp k6 run scripts/load/k6_profiles.js`
3. Run the money-invariant races (real parallel HTTP + post-run DB
   invariant checks; exit 0 = every invariant holds, 1 = violation, 2 =
   setup error, 3 = invariants unverifiable):
   `backend\venv\Scripts\python scripts\load\run_races.py run --phase all`
4. Stop the capture, read `manifest.txt`, review `sentry_issues.json`,
   fill the limits report template in the T2 plan.
5. Reset demo state between passes: `manage.py seed_demo --cleanup` then
   `manage.py seed_demo`.

Run the program only when no other live traffic is scheduled (serial-live
rule), only against TEST-mode Razorpay credentials, and never concurrently
with a customer demo. OOM on the UAT box during a stress pass is an
accepted, informative outcome — record it, do not mask it.
````

## Known constraints carried into execution

- The storefront login door refuses privileged accounts, and the
  `auth` throttle is per-ident: buyer provisioning and login pacing are
  operator work, not something the runner can shortcut.
- The `coupon` scope (10/min) meters the dedicated apply-coupon surface;
  the races carry the code in the checkout payload instead, which spends
  the `cart` scope — this is why the runner never calls apply-coupon.
- k6's double-capture leg replays a PRE-MINTED signature triple (minted by
  `run_races.py mint-signature`); the secret never appears on a k6 command
  line or in any committed file.
- The invariant SQL is PostgreSQL (the UAT engine). SQLite is not a
  supported target for the round-trip SQL.
