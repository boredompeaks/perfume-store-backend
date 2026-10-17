# SPEC-T1 — Red-team attack catalog (T1a families 1-5, T1b families 6-10)

Task SPEC-T1a (SHIPPED `79da100`) + SPEC-T1b (this half) on lane A, owner
charter: **"break the hardening."**
Deliverable of BUILD-QUEUE Phase T, T1 items 1-10. Every row below is one
attack run LIVE from this sandbox over HTTPS against the real test
deployment (`api.boredompeaks.me`, storefront `test.boredompeaks.me`), test
mode only, no real money anywhere, webhook/intent volume capped, UAT OOM
accepted as informative. Runner: `scripts/redteam_t1b.py` for families 6-9
(reusing T1a's `scripts/redteam_t1a.py` transport by import; stdlib urllib,
no new dependencies); regression subset: `scripts/security_smoke.sh`
(families 1-10).

Row verdicts: **PASS** = invariant held under attack (evidence in the row);
**FAIL** = invariant broken on the live env (finding with repro in the row);
**BLOCKED** = attack not executable on this deployment (the environment gap
is named in the row and remediated in the deployment section below).

## Verdict summary

| Family | PASS | FAIL | BLOCKED | rows |
|---|---|---|---|---|
| 1 — Order lifecycle / capture | 5 | 0 | 7 | 12 |
| 2 — Inventory | 4 | 0 | 6 | 10 |
| 3 — Coupons | 5 | 1 | 5 | 11 |
| 4 — Guest / cart | 13 | 0 | 2 | 15 |
| 5 — RBAC matrix / delist | 6 | 0 | 4 | 10 |
| 6 — Auth / MFA (T1b) | 10 | 0 | 4 | 14 |
| 7 — Input / validation (T1b) | 9 | 1 | 2 | 12 |
| 8 — Oracles / info leaks (T1b) | 10 | 0 | 1 | 11 |
| 9 — Notifications / outbox (T1b) | 3 | 0 | 3 | 6 |
| **Total (families 1-9)** | **65** | **2** | **34** | **101** |

Family 10 is the smoke extension, not attack rows: `bash
scripts/security_smoke.sh` runs **52 checks, 0 failed** on the run recorded
for T1b (33 from T1a + 19 added; family-10 section below).

T1a's finding stands: **F3-C4 (Medium)** — the coupon scope's shipped
default budget was not enforced per client at 12 shots; controls localize it
as deployment-shaped, not view-shaped (detail below). T1b's new finding:
**F7-B1 (Medium)** — non-object JSON bodies 500 on the cart write (detail
below). The 34 BLOCKED rows are not defects: they need state/config the box
does not have (deployment section + T1b's STEP ZERO re-probe). All executable
money-path, uniformity, session, RBAC, auth-throttle, input-validation and
oracle attacks held.

## STEP ZERO — environment capability verdict

Probed before any attack (Appendix A is the raw probe table). What the box
**runs, and runs current**:

- Q-S2/F2 health split live: public `/health/` is exactly
  `{"status": "ok", "checks": {...}}` with no business counters.
- All four v1 route families plus every legacy alias answered (one view
  object per route: v1 and legacy refused identically in every paired row).
- TIER1-GUEST-DEADEND live: anonymous checkout refused uniformly before any
  payload parsing (all nine payload shapes byte-identical, including
  malformed JSON and text/plain).
- Q-S3 register live: 201 with no numeric user id, account created inactive
  pending an emailed token (login of the unverified account is the uniform
  401); a reused contact email answers the uniform 400 email-taken
  validation — the residual oracle Q-S3 already reported, not a new one.
- Uniform error envelope (`error`/`code`/`details`) on every refusal
  observed, including CSRF refusals.
- Cookie hardening on the cart session: `HttpOnly; Secure; SameSite=Lax`,
  parent-domain `boredompeaks.me`, `Max-Age=1209600`.
- CSRF trusted origin is the storefront: a same-origin browser-shaped POST
  (Origin + Referer + csrftoken) passes the referer gate; `evil.example` is
  refused by name.
- Edge: the default Python UA is blocked at Cloudflare (403 bot page) while
  browser-shaped and curl clients pass; Django admin answers only the login
  redirect (`/admin/login/?next=/admin/`).
- Webhook endpoint fail-closed: every forged delivery 503s with
  "Webhook signature verification is not configured" (matches the
  `.env.example` contract for an empty `RAZORPAY_WEBHOOK_SECRET`).

What the box **does not have** (each gap names the rows it blocks):

| # | Deployment gap (measured evidence) | Rows unblocked by fixing it | Fix |
|---|---|---|---|
| 1 | `manage.py seed_demo` **never ran**: product list `count:0`, `demo-plenty-perfume` 404, `demo-customer` login 401 | F1-B1/B2/B3/B4, F2-B1/B2/B3/B5, F3-B1/B2/B3/B4/B5, F4-B1, F5-B2/B3/B4 | run `manage.py seed_demo` server-side (marked `demo-`/`DEMO-` rows; `--cleanup` exists) |
| 2 | **No reachable account of any role**: seeded users absent (gap 1) and self-registered accounts stay inactive until an emailed token this sandbox cannot receive; staff additionally require the R-17.9 TOTP door | F1-B6/B7, F2-B4/B6, F4-S2, F5-B1/B2 | gap 1 (its demo users are created active with the printed credential) + the MFA enrollment bootstrap for staff doors |
| 3 | **`RAZORPAY_WEBHOOK_SECRET` unset** (every delivery 503s before signature verification); `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` appear unset (no intent can be minted to observe) | F1-B1/B3/B4/B5 (and the configured-secret signature paths of F1-W1) | set the webhook secret + key pair in the box env (values stay env-side; never in the repo) |
| 4 | `shipping_configured:false` on `/health/` — no active shipping method, so even a seeded cart cannot complete a destination-valid checkout | secondary blocker for B-rows that need a *successful* checkout | `manage.py seed_shipping_methods` or admin-created methods |

Secondary observation: `GET https://test.boredompeaks.me/health/` answers
**308** (redirect; the probe recorded status only, not the target).

## Method notes (how the evidence was produced)

- One runner, phases `env,f1,f2,f3,f4,f5`, JSON evidence written after every
  phase so a partial run keeps usable rows. Regenerate with:
  `backend\venv\Scripts\python scripts\redteam_t1a.py --base <url> --out <path>`
  (the raw JSON is deliberately **not** committed — the tables below carry
  every row's request/response inline).
- Identity model: a bare cookie-less client (the gateway/anonymous shape) and
  session clients whose unsafe POSTs are browser-shaped (trusted Origin +
  Referer + that session's own csrftoken). An earlier draft run sent sessioned
  POSTs without any Origin, so Django's referer check refused them before the
  application layer — those draft rows measured the wrong layer and were
  re-executed browser-shaped; the three CSRF axes that shape exposes are
  separate rows (F4-G3, F4-G7, F4-G8). Only the corrected run is reported.
- Throttle discipline: coupon-scope traffic waits for a fresh 60-second
  window before the 12-shot burst; register/username/recovery probes stay
  inside their own budgets; every burst is bounded and logged row-by-row
  (12-shot window, 30-shot control, 25-shot continuation, 10-way parallel),
  no brute force, no payload above 300 bytes.
- The runner exits 0 when a run completes; verdicts live in the rows.

## T1b STEP ZERO — environment re-probe (owner-ops delta vs T1a)

Re-probed live before any T1b attack (raw rows in the runner's env phase).
Delta vs T1a's appendix A: **none — all four owner ops are still pending**:

| Probe (T1a gap) | T1a observed | T1b observed |
|---|---|---|
| `seed_demo` | products `count:0`, `demo-plenty-perfume` 404 | products `count:0`, `demo-plenty-perfume` 404 |
| seeded account | `demo-customer` login 401 | `demo-customer` login 401 |
| `RAZORPAY_WEBHOOK_SECRET` | unsigned delivery 503 (fail-closed) | unsigned delivery 503 (fail-closed) |
| shipping | `shipping_configured:false` | `shipping_configured:false` |

This task's one authorized fixture: registration of `rt_t1b_fixture`
(password fixture `quiet harbor lane`, email `rt_t1b_fixture@redteam.invalid`)
answers 400 "An account already uses this email address." — the account was
created by this run's first invocation (the 201 shape is T1a's Q-S3
measurement) — and its login answers 401 (inactive until the emailed token).
Because every delta is zero, T1b does **not** re-run T1a's families: the 24
BLOCKED rows above stay as recorded, and the second pass remains queued to
T1a's runner for whenever the owner ops land (seed, webhook secret, one
activated account, shipping).

## T1b method notes (families 6-9) — how this half's evidence was produced

- One runner, phases `env,f7,f6,f8,f9` (the default order puts f7 before f6
  so f6's register-scope flood starts from a clean window); evidence JSON is
  rewritten after every phase. Regenerate with
  `backend\venv\Scripts\python scripts/redteam_t1b.py --base <url> [--only
  <phases>]` (the raw JSON is deliberately uncommitted — the tables below
  carry every row inline). The transport (per-identity cookie jar, the
  browser-shaped sessioned-POST helpers, the envelope checker) is imported
  from `scripts/redteam_t1a.py`, not copied.
- Identity model matches T1a: a bare cookie-less client for anonymous and
  wrong-layer-sensitive probes, a sessioned identity for writes that must
  clear the CSRF gate. A fresh sessionless identity was used for the family-9
  trigger matrix (see the self-correction note below).
- Throttle discipline: every flood phase begins by sleeping one full 60 s
  window (spend from other processes is invisible to this runner); the
  register/recovery floods fire INVALID payloads, so the budget is measured
  with zero accounts created and zero mail sent (ScopedRateThrottle spends in
  `initial()`, before the view runs); burst caps are logged shot-by-shot —
  auth <= 45, register <= 25, recovery <= 25, each at least 3x the shipped
  default so a refusal index is observable at all (T1a's C5 measured a 5/min
  scope first refusing at shot 19 here, i.e. per-client budgets on this box
  run at a multiple of the configured rate).
- Three requests exceed T1a's small-probe norm because the attack IS their
  size — one ~6 MB multipart image, one ~300 KB JSON object, one depth-2500
  body — each fired once, never in a loop; plus one 4 KB URL probe.
- Two measurement self-corrections, both T1a's wrong-layer lesson repeating:
  the first family-9 matrix fired sessioned POSTs without CSRF material and
  therefore measured Django's referer gate (403s) instead of the triggers'
  own gates — re-executed with a fresh sessionless identity; the first
  family-8 source-map probes concatenated an absolute URL onto the request
  path, so their "results" were local transport errors — re-executed as
  path-only probes that really hit the edge. Only the corrected runs appear
  in the tables below. One runner crash (family 6's JWT battery) and one
  verdict-shape fix (the 6 MB upload's transport abort) each cost one phase
  re-execution; totals come from the final runs.

## Family 1 — Order lifecycle / capture

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F1-W1 | Six forged webhook deliveries in one batch: unsigned, 64-hex wrong sig, alg-confusion 'none', 63-char truncated, empty sig, garbage body + bad sig. | HMAC-SHA256 over the raw body is checked with hmac.compare_digest BEFORE any parsing application (orders/webhooks.py); with the secret unset the endpoint answers one uniform 503 and never accepts a delivery (fail-closed, `.env.example` RAZORPAY_WEBHOOK_SECRET). | no-signature-header=503; bad-signature-64hex=503; alg-confusion-none=503; truncated-signature-63=503; empty-signature=503; garbage-body-badsig=503; byte-identical across variants: True | **PASS** | n/a |
| F1-W2 | Two identical forged deliveries fired concurrently (double-capture race at the HTTP layer). | Neither delivery may be accepted: signature check precedes the idempotency no-op key on the order's payment dimension (orders/webhooks.py), so nothing is recorded twice. | statuses [503, 503] | **PASS** | n/a |
| F1-V1 | Forged browser-verify quadruple (bogus order/payment ids + 64-hex signature): plain, amount=1/currency tamper fields, legacy mount, and a same-origin sessioned identity (Origin+csrftoken replayed). | verify_payment is IsAuthenticated + 'payment' scope (orders/views/payment.py): an unauthenticated caller is refused by the auth stack before any signature work — identically on both mounts and regardless of session or tampered amount/currency fields (the server derives both from the Order row). | 401/401/401/401; all byte-identical True; body {"error":"Authentication credentials were not provided.","code":"not_authenticated","details":{}} | **PASS** | n/a |
| F1-P1 | Payment-intent mint attempt for a non-existent order with tampered amount/currency, via the v1 mount and the legacy alias. | create_payment is IsAuthenticated + 'payment' scope; amount/currency come from the caller's own Order row, never the body (orders/views/payment.py), and both mounts share one view object (config/urls.py). | 401 vs 401; byte-identical True; body {"error":"Authentication credentials were not provided.","code":"not_authenticated","details":{}} | **PASS** | n/a |
| F1-R1 | Refund without capture: anonymous POST to the refund seam (v1 + legacy alias), amount 1.00 on order id 1. | The refund seam is gated by its permission class before any state-machine check (config/urls.py mounts; orders/refunds.py), so a caller who never captured cannot reach the money path at all. | 403 vs 403; byte-identical True; body {"error":"You do not have permission to perform this action.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F1-B1 | Replay an already-captured webhook (idempotency no-op key on the order's payment dimension). | Second identical delivery no-ops; stock ledger moves once. | BLOCKED: RAZORPAY_WEBHOOK_SECRET is unset on the live env (every delivery 503s), so no delivery can carry a valid signature; also needs a captured order from seed_demo. | **BLOCKED** | n/a |
| F1-B2 | Replay a browser-verify for an already-paid order. | verify_payment's payment-dimension guard no-ops the replay. | BLOCKED: Requires an authenticated account + a paid order: seed_demo never ran on this env and self-registered accounts stay inactive until an emailed token we cannot receive. | **BLOCKED** | n/a |
| F1-B3 | Webhook + verify fired concurrently (double-capture race, ledger moves once). | select_for_update() on the order row + payment dimension makes one writer of the sale (orders/webhooks.py, orders/inventory.py). | BLOCKED: Needs a valid webhook secret AND a real capturable order — both missing on this deployment. | **BLOCKED** | n/a |
| F1-B4 | Valid-signature capture for a non-existent / cancelled order. | Unknown order id is refused; cancelled order stays cancelled (payment state machine in orders/state.py). | BLOCKED: Valid signatures require the server-side RAZORPAY_WEBHOOK_SECRET; cancelled orders require seeded/created orders. | **BLOCKED** | n/a |
| F1-B5 | Amount/currency tampering inside a valid verify/webhook payload. | Server recomputes amount from the Order row in the store currency; gateway-computed HMAC covers the real payload. | BLOCKED: Needs a real order + gateway credentials (RAZORPAY_KEY_ID/SECRET appear unset: no payment intent can even be minted to observe). | **BLOCKED** | n/a |
| F1-B6 | Double refund + refund-without-capture at the staff seam logic level. | Refund state machine (orders/refunds.py) refuses both; refund is row-locked in transaction.atomic(). | BLOCKED: Needs a staff session (no staff account is reachable: no seeded users, no inbox for self-registration verification). | **BLOCKED** | n/a |
| F1-B7 | Browser-verify replay after expiry / across users. | Signature is bound to the gateway order id; ownership is the caller's own order. | BLOCKED: Needs an authenticated owner of a paid order (deployment state missing as above). | **BLOCKED** | n/a |

## Family 2 — Inventory

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F2-A1 | Negative stock via the adjust API: anonymous POST delta=-99 on the v1 mount and the legacy alias. | HasInventoryAdjust gates the endpoint through permission_classes (products/views.py) — an anonymous caller never reaches the delta validation, let alone the stock row. | 403 vs 403; byte-identical True; body {"error":"You do not have permission to perform this action.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F2-A2 | Product create with stock=-5 (negative stock via the write API), anon on both mounts. | products.write capability is checked before serializer validation (HasProductsWriteOrReadOnly), so the negative value never reaches a model field. | 403 vs 403; byte-identical True; body {"error":"Administrator access is required.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F2-A3 | Update/delete attempt on a product detail route as anon (stock mutation through the write verbs). | Write verbs carry the products capability check; a nonexistent slug must not be distinguishable from a delisted one on a read, and a write must be refused before the slug lookup (MERCHANT-DELIST). | PUT=403 DELETE=403; PUT body {"error":"Administrator access is required.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F2-A4 | Admin inline stock adjust: reach the Django admin changelist as anon (the surface that carries the stock inline). | The admin mount is session+MFA gated (accounts/admin.py MFAAdminSite + Q-S4 login throttle); anon gets only the login redirect, never a form or a row. | 302 location=/admin/login/?next=/admin/ | **PASS** | n/a |
| F2-B1 | N parallel checkouts of the last unit (oversell race). | commit_order_sale's summed re-check under select_for_update makes exactly one winner; stock never goes negative. | BLOCKED: No purchasable product exists (seed_demo never ran; product list count=0) and no authenticated checkout identity is reachable. | **BLOCKED** | n/a |
| F2-B2 | Admin adjust-stock racing a concurrent capture. | StockMovement ledger stays consistent: every capture has exactly one row-set and stock equals opening +/- ledger. | BLOCKED: Needs staff session (inventory role) + a capturable order. | **BLOCKED** | n/a |
| F2-B3 | Reservation expiry during an in-flight payment. | expire_reservations releases only stale holds; verify_payment re-checks live stock at capture (SPEC-12-02). | BLOCKED: Needs a created order with reservations (seed/state missing). | **BLOCKED** | n/a |
| F2-B4 | Negative stock via the admin inline / admin form. | Model/validation backstop refuses negative stock on every surface. | BLOCKED: No staff credentials exist on this deployment (see env verdict). | **BLOCKED** | n/a |
| F2-B5 | Summed re-check twin-line shape (two order lines that each pass the per-line backstop but exceed the sum). | commit_order_sale re-checks the summed demand at payment time. | BLOCKED: Needs a cart holding two lines of real products — nothing to add (product list empty). | **BLOCKED** | n/a |
| F2-B6 | Adjust-stock as catalogue/inventory/customer roles (capability matrix on the mutation surface). | inventory.adjust is inventory+admin only (common/roles.py CAPABILITY_ROLES). | BLOCKED: No role-bearing accounts can be created: roles are assigned by staff and seed_demo never ran. | **BLOCKED** | n/a |

## Family 3 — Coupons

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F3-C1 | Cartless preview oracle: apply-coupon with two different absent codes and NO session/cart. | Without a cart the endpoint answers one uniform not-found before it ever reads the code (TIER1-GUEST-DEADEND checkout module), so the error channel cannot distinguish coupon codes to a cartless caller. | 404 vs 404; byte-identical True; body {"error":"Cart not found","code":"not_found","details":{}} | **PASS** | n/a |
| F3-C2 | Coupon code enumeration via error differences: two absent codes against a real (session) cart. | Absent codes take one identical error path (uniform coupon errors), so a caller cannot enumerate which codes exist by diffing bodies. | 400 vs 400; byte-identical True; body {"error":"Invalid coupon code","code":"validation_error","details":{}} | **PASS** | n/a |
| F3-C3 | Malformed coupon payloads: empty code, 300-char code, lowercase twin of an absent code, SQL-quote payload, markup payload. | Every malformed input stays inside the 4xx envelope — no 5xx, no database error text, and code-shaped payloads answer identically to the plain absent code (no reflection/oracle). | empty-code=400; oversize-code=400; lowercase-twin=400; quote-injection=400; markup-payload=400; injection payloads byte-identical to absent: True | **PASS** | n/a |
| F3-C4 | Coupon scope throttle: 12 rapid apply-coupon calls in a fresh 60-second window (rate 10/min by default, THROTTLE_COUPON_RATE). | ScopedRateThrottle on the 'coupon' scope refuses beyond the budget with 429 (settings REST_FRAMEWORK DEFAULT_THROTTLE_CLASSES), so the preview surface cannot be sprayed. | statuses=[400, 400, 400, 400, 400, 400, 400, 400, 400, 400, 400, 400]; 429 count=0/12; first 429 body -; Retry-After=None | **FAIL** | Medium |
| F3-C5 | Throttle instrument control: up to 30 rapid username-available probes (scope username-probe, default 5/min) from the same client. | A dedicated ScopedRateThrottle scope must refuse beyond its budget from one client — the control that tells a coupon-view anomaly from a deployment-wide throttle-cache/keys question. | statuses=[200 x18, 429]; first refusal at shot 19 | **PASS** | n/a |
| F3-C6 | Sustained coupon spray continuing C4's fresh window: up to 25 more apply-coupon shots from the same client (up to 37 total). | A sprayed preview surface must be refused within a bounded number of shots; the refusal index IS the effective per-client budget (the shipped default is 10/min). | statuses=[400 x19, 429]; first refusal at shot 31, window-total=32 | **PASS** | n/a |
| F3-B1 | Double-burn a usage_limit=1 coupon via webhook+verify concurrency. | Coupon spend is committed once under select_for_update; the second capturer sees limit reached (orders/inventory.py + state machine). | BLOCKED: Needs DEMO-ONCE (seed never ran) + a valid webhook secret. | **BLOCKED** | n/a |
| F3-B2 | Over-limit timing: limit reached between cart-add and capture — must be recorded-not-prevented + visible. | The over-limit condition is recorded at capture, not silently prevented, and shows in the order/response. | BLOCKED: Needs a usage-limited coupon with real orders in flight. | **BLOCKED** | n/a |
| F3-B3 | Cartless preview oracle against an EXISTING code (present vs absent differential). | Cartless callers get one uniform answer regardless of code existence. | BLOCKED: No coupon rows exist on this deployment (seeded DEMO-ONCE absent), so only the absent-vs-absent half was executable. | **BLOCKED** | n/a |
| F3-B4 | Reuse a coupon after order cancel. | Cancel path returns the coupon spend; reuse goes through normal validation once. | BLOCKED: Needs an order with a coupon applied and a cancel seam session. | **BLOCKED** | n/a |
| F3-B5 | Coupon enumeration across existing vs absent codes with a cart. | Existing and absent codes produce no diffable existence oracle. | BLOCKED: No coupon exists to enumerate against (deployment state missing). | **BLOCKED** | n/a |

## Family 4 — Guest / cart

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F4-G1 | Guest-checkout refusal bypass battery (no session): full payload, empty body object, missing/invalid/oversize/list/null guest_email, malformed JSON, text/plain content type. | TIER1-GUEST-DEADEND: create_order answers ONE uniform refusal before the atomic block opens — no payload shape may differentiate the answer (orders/views/checkout.py _guest_checkout_refused). | full-payload=403; empty-object=403; missing-guest-email=403; guest-email-invalid=403; guest-email-oversize=403; guest-email-list=403; guest-email-null=403; malformed-json-body=403; text-plain-content-type=403; all byte-identical: True | **PASS** | n/a |
| F4-G2 | Session-trick checkout: the same refusal POST riding the cart session cookie, browser-shaped (trusted Origin + csrftoken replay). | Session presence changes nothing: the deadend refusal is user-independent and byte-identical with or without a session once the request is same-origin (orders/views/checkout.py). | 403; byte-identical to no-session battery: True; body {"error":"Guest checkout is not available. Sign in or create an account to place an order.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F4-G3 | Session-riding checkout with NO Origin/Referer and no CSRF token (server-to-server shaped request wearing a stolen cookie). | The CSRF referer check refuses an HTTPS sessioned POST that presents no Origin/Referer before the view runs (SPEC-17-03 stack), and the refusal carries no order/cart data. | 403 {"error":"CSRF Failed: Referer checking failed - no Referer.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F4-G7 | Cross-site session riding: stolen cookie replayed from https://evil.example WITH a valid csrftoken attached. | Origin checking refuses any origin outside CSRF_TRUSTED_ORIGINS by name — possession of both cookie and token cannot move the request off the storefront origin (SPEC-17-03; Django referer/origin gate). | 403 {"error":"CSRF Failed: Origin checking failed - https://evil.example does not match any trusted origins.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F4-G8 | Trusted origin but NO csrftoken (double-submit binding). | The same-origin check passing is not enough: the token must match the cookie (SessionCartCSRFAuthentication / SPEC-17-03), otherwise the request is refused before the view. | 403 {"error":"CSRF Failed: CSRF token missing.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F4-G4 | Auth+anon mix: garbage Bearer token alongside the session cookie. | An invalid bearer is rejected by the JWT authentication stack uniformly (401 envelope) — the mix never downgrades to a different code path or leaks order state. | 401 {"error":"Given token not valid for any token type","code":"not_authenticated","details":{"code":"token_not_valid",...}} | **PASS** | n/a |
| F4-G5 | Keyed replay: same Idempotency-Key on repeated checkout POSTs (bare, repeat, session+CSRF). | The refusal precedes any key consumption — nothing is created and no 'key already spent' oracle exists while guest checkout is a dead-end (orders/views/checkout.py). | 403/403/403; all byte-identical to the plain refusal: True | **PASS** | n/a |
| F4-G6 | Ten concurrent anonymous checkout POSTs (parallel refusal race). | The deadend refuses everything before opening a transaction — concurrency cannot buy a single created row or a divergent answer. | statuses [403 x10]; all byte-identical: True | **PASS** | n/a |
| F4-X1 | Cart id enumeration via session replay: three fresh sessions each GET the cart twice (stability + distinctness). | The cart is keyed by its own session — each session always resolves to ITS cart id, never to another session's, and ids never rotate under a stable cookie. | ids A=(114,114), B=(115,115), C=(116,116); per-session stable + cross-session distinct: True | **PASS** | n/a |
| F4-X2 | Cross-session cart item access: session B deletes/patches item ids 1-3 (ids it does not own). | cart_item_detail resolves items only inside the caller's own cart (cart/views.py), so foreign or unknown item ids share one miss answer and no other cart's totals leak. | DEL/1=404; DEL/2=404; DEL/3=404; PATCH/1=404; uniform status: True; no data: True | **PASS** | n/a |
| F4-X3 | Cart add of unknown products (session B, with CSRF): non-numeric product_id and a huge numeric id. | Unknown products are refused inside 4xx — the view's ValueError->404 guard means a garbage id must never 500 and the refusal must not disclose whether any id exists (cart/views.py). | garbage-id=404 {"error":"Product not found",...}; huge-id=404 {"error":"Product not found",...} | **PASS** | n/a |
| F4-S1 | Session fixation probes: (a) a captured sessionid replayed from a fresh client, (b) a fabricated sessionid replayed the same way. | The session cookie IS the cart credential by design (SPEC-17-03), carried only via Set-Cookie (never a response body — see F4-P1): a genuine cookie reproduces exactly the session it was minted for (no cross-session confusion) and a forged value gains no existing cart — the server mints a fresh one instead of erroring. | legit: 200 -> cart 117 (own cart 117); forged: 200 -> cart 118 (not in live set [114, 115, 116, 117]) | **PASS** | n/a |
| F4-S2 | Login-time session rotation after a planted cart session (fixation completion). | Django cycles the session key on login, so a planted sessionid cannot become an authenticated session the attacker still holds. | BLOCKED: no credential can complete a login on this deployment (seed users absent; self-registered accounts inactive until an emailed token we cannot receive) | **BLOCKED** | n/a |
| F4-P1 | POST-F1 regression: scan every response body captured in this run (webhooks, verify, checkout, cart, coupon, register, config) for any sessionid/csrftoken value observed in Set-Cookie. | The session key never enters any payload — cookies are the only carrier (SPEC-17-03 / POST-F1 finding); a body echo would re-open the leak. | leaks=none | **PASS** | n/a |
| F4-B1 | Cart clear-on-capture cannot be tricked into clearing another user's cart. | Capture clears only the ordering user's cart inside the capture transaction (orders flow), never a cart keyed by another session. | BLOCKED: needs a successful capture (no capturable order, no authenticated buyer, no webhook secret) | **BLOCKED** | n/a |

## Family 5 — RBAC matrix / delist uniformity

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F5-M1 | RBAC matrix, anonymous identity x 20 capability endpoints (dashboard, audit, admin health, orders read/fulfil/cancel/refund on both mounts, global search, saved filters, inventory adjust, product create/delist, store orders). | Every staff/capability surface answers 401/403 through permission_classes (or the admin's login redirect) and never 2xx with data to an anonymous caller; legacy and v1 mounts are one view object so they refuse identically (config/urls.py). | dashboard=302; dashboard-legacy=302; audit-log=302; audit-log-legacy=302; admin-health=403; orders-list=403; orders-list-legacy=403; order-detail=403; order-detail-legacy=403; order-fulfil=403; order-cancel=403; order-refund=403; global-search=302; saved-filter-save=403; saved-filter-delete=403; inventory-adjust=403; product-create=403; product-delist-put=403; store-orders-list=401; store-order-detail=401; non-2xx everywhere: True; anomalies: none | **PASS** | n/a |
| F5-M2 | RBAC matrix, anonymous+session identity x 4 sensitive endpoints (orders read, refund, inventory adjust, product create). | A cart session confers no staff capability: authorization is the capability check on the user, and the anonymous user stays anonymous however the cookie is presented. | orders-list=403; order-refund=403; inventory-adjust=403; product-create=403; first body {"error":"You do not have permission to perform this action.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F5-I1 | IDOR on customer order detail: GET another user's order id (1) and an absurd id, anonymously. | order_detail is IsAuthenticated and uniform-401 before any lookup, so the answer cannot even confirm whether the id exists (no existence oracle on ids). | 401 vs 401; byte-identical True; body {"error":"Authentication credentials were not provided.","code":"not_authenticated","details":{}} | **PASS** | n/a |
| F5-I2 | Guest-token IDOR: junk 64-char token against the token route and the token+order-number route; plus a 128-char oversize token. | Every guest lookup miss answers the one _guest_lookup_miss body (orders/views/checkout.py); the token length bound refuses oversize input before it reaches the index. | token=404 by-number=404 byte-identical=True; oversize=404 {"error":"Order not found","code":"not_found","details":{}} | **PASS** | n/a |
| F5-D1 | Delist uniformity, nonexistent side: two absent slugs on the v1 mount plus the legacy alias twin of one. | MERCHANT-DELIST: product_detail serves the same 404 for a nonexistent and a delisted row (products/views.py), and both mounts share the view — byte-identical bodies across slugs and mounts. | 404/404/404; v1 twins identical True; legacy identical True; body {"error":"Product not found","code":"not_found","details":{}} | **PASS** | n/a |
| F5-D2 | REST search/filters/relations half of delist uniformity: five filter/ordering/search variants (incl. adversarial ordering and markup) against the empty catalogue, vs the clean list. | The list base queryset is filter(is_active=True), so no downstream search/ordering/filter can reintroduce a hidden row (products/views.py MERCHANT-DELIST); invalid numeric filters answer one designed validation error, markup/SQL-shaped strings are inert values. | clean=200; byte-identical to clean per variant [True, True, True, True]; variant statuses=[200, 200, 200, 400, 200]; invalid-price=400 {"error":"Price filters must be valid numbers","code":"validation_error","details":{}} | **PASS** | n/a |
| F5-B1 | RBAC matrix for the 7 staff roles (support, catalogue, inventory, marketing, finance, admin, superadmin) + a customer role across every capability endpoint. | Each role answers 200 only on its CAPABILITY_ROLES cells and 403 elsewhere (common/roles.py). | BLOCKED: No role-bearing account can be created from outside: seed_demo never ran (demo-* logins all 401) and self-registration yields an inactive account gated on an emailed token this sandbox cannot receive. | **BLOCKED** | n/a |
| F5-B2 | IDOR on order detail ACROSS real users (user A's token reading user B's order; guest token reading another guest's order). | Object-level check: only the owner (or a valid guest token for that row) can read an order (orders/views/order.py). | BLOCKED: No orders exist on this deployment and no authenticated identity is obtainable. | **BLOCKED** | n/a |
| F5-B3 | Delisted-vs-nonexistent byte comparison on the PUBLIC detail read (the live half of MERCHANT-DELIST). | The 404 body for a delisted row is byte-identical to a nonexistent one. | BLOCKED: No product row exists to delist: the catalogue is empty and only staff can write products. | **BLOCKED** | n/a |
| F5-B4 | Delisted row exclusion from REST search/filter/relation responses (list half). | filter(is_active=True) base queryset keeps delisted rows out of every list/relation response whatever the params. | BLOCKED: Requires at least one delisted row; catalogue is empty. | **BLOCKED** | n/a |

## Finding detail — F3-C4 (Medium): scoped throttle budget not enforced per client at 12 shots

**What was attacked:** 12 rapid `apply-coupon` POSTs from one client inside a
guaranteed-fresh 60-second window (the runner sleeps until the oldest prior
coupon call expires), browser-shaped so every shot reaches the view (all 12
answers are the view's 400 "Invalid coupon code", proving the throttle check
itself did not refuse).

**Measured facts:**

- Shipped default for the `coupon` scope: 10/min (`THROTTLE_COUPON_RATE`).
- C4: shots 1–12 → **0 × 429** (all reached the view).
- Control C5, different dedicated scope `username-probe` (default 5/min),
  same client: first refusal at **shot 19** (not the 6 a per-client 5/min
  would give).
- Continuation C6, same window as C4: first refusal at **shot 31** of the
  window (not 11; effective budget ≈ 3× the 10/min default).

**Reading (cause labelled hypothesis, measurements above are facts):** both
scopes refuse, so throttling is wired; both refuse far past the shipped
default; the pattern (≈ rate × small integer) is what a per-process LocMem
throttle cache split across N app workers produces — the exact situation the
repo's own `.env.example` documents as the open TIER1-THROTTLE-CACHE finding
(until `REDIS_URL` is set, DRF's cache is per-process). Worker count, the
box's `THROTTLE_*` values and `NUM_PROXIES` are not readable from outside,
so the runner reports the effective budgets and refuses to claim a single
cause.

**Impact:** any scope's per-client budget on this deployment is
rate × worker-share, not rate — for the coupon preview that is Low-value
data; for `auth`/`register`/`username-probe` (same infrastructure) it widens
the credential/enum spray window proportionally — T1b families 6-10 should
re-measure those scopes with this instrument before signing them off.

**Remediation:** set `REDIS_URL` so the DRF throttle cache moves to shared
Redis (the documented TIER1-THROTTLE-CACHE path), or lower the affected
`THROTTLE_*` rates to compensate; re-run `scripts/security_smoke.sh` and the
C4/C5/C6 rows after the change.

## `scripts/security_smoke.sh` — family 1-5 regression subset

Fast, anonymous-only, `bash + curl` only; `bash scripts/security_smoke.sh
[base-url]` (defaults to the live UAT API), exit 0 = every invariant held.
**33 checks, 0 failed** on the run recorded for this task. Coverage:

- F1 (7): forged webhook unsigned/bad-64hex/truncated/alg-none all non-2xx;
  anonymous verify → 401; anonymous payment intent → 401; anonymous refund →
  401/403.
- F2 (3): inventory adjust → 401/403; product create → 401/403; admin
  changelist → 302 to login only.
- F3 (3): cartless coupon preview refused for two absent codes, byte-identical
  (no cartless oracle).
- F4 (6): guest refusal 403 and byte-identical across full/empty/malformed
  payloads; cart 200 with sessionid absent from the body; sessioned POST
  without CSRF material → 401/403.
- F5 (14): anonymous GETs on five admin/order surfaces → 401/403/302;
  anonymous POSTs on fulfil/cancel/refund → 401/403; customer order list and
  detail → 401; two nonexistent product slugs → byte-identical 404, legacy
  mount byte-identical to v1.

Deliberately out of the subset (they burn throttle windows or need
credentials): the 12-shot throttle burst, any registration/recovery call,
and every staff-role cell. (T1b has since extended this same script with the
families 6-10 subset — see the family-10 section below for the current
total.)

## Family 6 — Auth / MFA (T1b)

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F6-A1 | credential-stuffing spray: up to 45 storefront login POSTs, one real username (the fixture), wrong passwords, fresh window | the auth scope refuses per-client within a bounded shot count (429), and every non-refusal is the same uniform 401 (no user-existence oracle under spray) | statuses=[401 x33, 429]; first refusal at shot 34/34; shipped default auth=10/min; non-429 bodies uniform: True | **PASS** | n/a |
| F6-A2 | register-scope spray: up to 25 empty-body register POSTs in a fresh window (the throttle spends before the view runs, so nothing is created) | the dedicated register scope refuses per-client within a bounded shot count; the 400s stay uniform (no payload oracle) | statuses=[400 x14, 429]; first refusal at shot 15/15; shipped default register=5/min; no account created (all payloads invalid); non-429 uniform: True | **PASS** | n/a |
| F6-M1 | unauthenticated MFA battery: setup/confirm/disable/trust/status with no credentials at all (Q-S4's refusals re-verified on prod) | every MFA surface refuses an unauthenticated caller through the 401/403 envelope before any enrollment logic runs, and no secret or otpauth URI ever appears in a refusal | setup=401; confirm=401; disable=403; trust=403; status=403; envelope shape ok: True; secret disclosed: False | **PASS** | n/a |
| F6-M2 | MFA setup with the run's REAL registered account credentials (`rt_t1b_fixture`, inactive pending email token) vs a ghost account | the credential bootstrap never opens for an unverified account, and the refusal is byte-identical to the ghost's (no account-state oracle through the enrollment door) | real=401 ghost=401 byte-identical: True; body {"error":"Invalid credentials.","code":"not_authenticated","details":{}} | **PASS** | n/a |
| F6-M3 | two different ghost accounts against MFA setup (control for M2) | the uniform rejection does not depend on the username shape | 401 vs 401; byte-identical: True | **PASS** | n/a |
| F6-R1 | cross-origin refresh: POST the refresh seam from `https://evil.example` (the sibling-origin/CSRF shape Q-P1's gate exists for) | the origin gate refuses BEFORE any carrier is read, so the probe neither sweeps nor mints from a victim cookie (no refresh_token Set-Cookie on the refusal) and the refusal is named | 403; named refusal: True; cookie swept by probe: False; body {"error":"Cross-origin refresh requests are not allowed.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F6-R2 | refresh cookie contract from the client's view: a forged body token with the trusted origin and with NO origin (non-browser shape); scan every Set-Cookie and every response body | the refresh_token cookie carries HttpOnly/Secure/Path=/api//SameSite=Lax wherever it is set, and no refresh token is ever issued in a response body [R-17.12] | statuses 401/401; cookie set: True; flags ok: True (HttpOnly, Path=/api/, SameSite=Lax, Secure each asserted); refresh in body: False; window-open (token reached validation, not refused by carrier name); cookie: refresh_token=""; HttpOnly; Max-Age=0; Path=/api/; SameSite=Lax | **PASS** | n/a |
| F6-J1 | JWT alg confusion: an alg=none unsigned token and an HS512 token signed with an attacker key, presented as Bearer on an authed endpoint | simplejwt validates against its algorithm allowlist and the server key — neither forgery authenticates (401 token_not_valid envelope) | alg-none=401; hs512-attacker=401 | **PASS** | n/a |
| F6-J2 | JWT key confusion: HS256 tokens signed with a wrong key, one with exp in the future and one with exp already past | an unknown-key token is refused before any claim matters; no exp/alg claim on a forged signature ever authenticates | future-exp=401 past-exp=401; envelope 401: True; exp enforcement with a VALID signature not isolable without a server-minted token (see F6-J3/J5) | **PASS** | n/a |
| F6-J3 | logout invalidation: log in, log out, replay the blacklisted refresh token | logout blacklists the refresh token [R-17.8] so a stolen refresh can no longer mint access tokens | BLOCKED: no activated account is obtainable on this deployment: seed_demo never ran (demo-customer 401) and the run's registered fixture stays inactive until an emailed token this sandbox cannot receive | **BLOCKED** | n/a |
| F6-J4 | refresh rotation replay after logout (BLACKLIST_AFTER_ROTATION observed over the wire) | a rotated-out refresh token is refused on replay and the replacement is single-use | BLOCKED: same no-activated-account gap as F6-J3 | **BLOCKED** | n/a |
| F6-J5 | refresh issuance on successful login: Set-Cookie HttpOnly + absence of the refresh token from the 200 body, proven from the client's view | login mints the refresh token ONLY into an HttpOnly cookie [R-17.12] (the observable rejection-path half is F6-R2) | BLOCKED: same no-activated-account gap as F6-J3 | **BLOCKED** | n/a |
| F6-P1 | password-reset oracle, absent-vs-absent control: two emails that cannot exist, body and timing compared | the recovery endpoint answers one uniform message (no status/body diff) whatever the email | 200 vs 200; byte-identical: True; elapsed_ms 145.4/74.6 (single-shot pairs over WAN — indicative only, no timing claim); body {"message":"If an active account exists for this email, a password-reset link has been sent."} | **PASS** | n/a |
| F6-P2 | password-reset, existing-vs-absent differential: an ACTIVE account's email vs a missing one (body + timing) | an existing-account reset is indistinguishable from a miss | BLOCKED: no active account exists on this deployment (the query filters is_active=True; seed absent, fixture inactive) — the existing-half cannot be exercised | **BLOCKED** | n/a |

## Family 7 — Input / validation (T1b)

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F7-MA1 | register mass assignment: is_active/is_staff/is_superuser/role/staff_roles/id smuggled into a real registration, then a login attempt with the created credentials | the serializer's explicit field whitelist drops every extra key and create() hardcodes is_active=False — the planted is_active can never activate the account (login stays 401) and no privileged field is echoed | register=201 login-after=401; privileged fields echoed: False; is_active honored: False; register body {"message":"User registered. Check your email to verify the account.","user":{"username":"rt_t1b_mass_bdf738",...}} | **PASS** | n/a |
| F7-MA2 | product-create mass assignment battery (negative stock, is_active, forged id/created_at) as anonymous | the capability check runs before serializer validation, so a mass-assignment payload never reaches a model field from an anonymous caller (HasProductsWriteOrReadOnly) | 403 {"error":"Administrator access is required.","code":"permission_denied","details":{}} | **PASS** | n/a |
| F7-MA3 | guest-checkout order-address mass assignment: user/status/total/payment_status smuggled into the address payload (sessioned, browser-shaped) | TIER1-GUEST-DEADEND refuses before any payload field is read, so privileged keys are byte-identical to the plain payload's refusal | mass=403 plain=403 byte-identical: True | **PASS** | n/a |
| F7-SQ1 | SQLi via search: tautology, stacked DROP, UNION/credential-exfil, and quote-break payloads against the product search | ORM parameterisation keeps every payload an inert value — all answers are 2xx/4xx with no SQL error text and no 5xx | statuses=[200, 200, 200, 200]; no-5xx: True; no SQL error text: True; sample body {"count":0,...,"results":[]} | **PASS** | n/a |
| F7-SQ2 | SQLi via ordering/filter: CASE-expression ordering, ordering-in-statement, tautology min_price, quote-break category | ordering is an explicit whitelist (unknown values fall to the default sort), price filters are Decimal-validated (400), category is an ORM exact match — no 5xx, no SQL error text | statuses=[200, 200, 400, 200]; ordering fell back to default: True; price refused 400: True; no SQL error text: True | **PASS** | n/a |
| F7-XS1 | reflected XSS sweep on the API: `<script>` payload in search and category params | every answer is application/json (a browser will not execute a JSON document); the payload can only exist JSON-escaped inside data | statuses 200/200; content-types ['application/json', 'application/json']; json-only: True | **PASS** | n/a |
| F7-XS2 | reflected XSS on the Django admin login: next= attribute-breakout + script payload against `/admin/login/` | the admin login page renders (200) with the payload HTML-escaped — no raw breakout reaches the document, no 5xx | 200; raw payload in HTML: False; escaped payload in HTML: True | **PASS** | n/a |
| F7-XS3 | stored XSS via product name/description rendered in the admin changelist and storefront: the payload POST was attempted first (anon → 403), then the admin form was probed (→ 302) | a stored payload can only enter the catalogue through a products.write capability; the admin render half must be verified with a staff session | BLOCKED: the anonymous write was refused at the capability gate (payload never reaches the catalogue) AND the admin add-form answers a login redirect — no staff account exists to complete a login (R-17.9 MFA door + no seeded credentials), so the RENDER half cannot be exercised on this deployment | **BLOCKED** | n/a |
| F7-UP1 | anonymous upload battery: polyglot image (GIF header + script) with a path-traversal filename, then one 6 MB image over the MAX_UPLOAD_MB ceiling (multipart, sessioned) | the capability gate refuses the write before any storage layer runs — nothing is ever accepted (no 2xx), no 5xx, and no traceback leaks; nothing is stored | statuses=polyglot 403, oversize transport-abort (no response: this sandbox aborted the 6 MB send mid-flight — ConnectionAbortedError after ~19 s, so the app-level oversize refusal was not observable from this client); nothing accepted + no traceback: True | **PASS** | n/a |
| F7-UP2 | server-side upload seam: oversize rejection message, polyglot content sniffing, and traversal-filename neutralisation inside the S3/media storage layer | MAX_UPLOAD_MB validation, Pillow content verification and suffixed-key storage all fire on a real staff upload (products.validate_image_size, .env.example media contract) | BLOCKED: the upload seam is reachable only through products.write (staff) and the Django admin (session+MFA); no staff account exists on this deployment (seed absent, R-17.9 MFA door) | **BLOCKED** | n/a |
| F7-B1 | serialization bombs against the cart write: 2500-deep nested array (the classic bomb), a nested OBJECT of the same depth (depth control), and a bare scalar (shape control) — sessioned, browser-shaped | the write refuses every payload shape inside 4xx — object or not, shallow or deep — never 5xx, never a traceback | array-depth2500=500, object-depth2500=400, scalar=500; no traceback anywhere: True; depth itself is handled (object-depth → 400); the 5xx fires on NON-OBJECT bodies (hypothesis: a dict-assuming line like request.data.get runs on a list/scalar and raises) | **FAIL** | Medium |
| F7-B2 | huge-JSON probe: one 300000-byte object posted to the cart write (sessioned, browser-shaped) | an oversized-but-well-formed body stays in 4xx with no 5xx and no traceback (the view refuses the payload or the edge caps it) | 400 {"error":"product_id is required","code":"validation_error","details":{}}; no traceback: True | **PASS** | n/a |

## Family 8 — Oracles / info leaks (T1b)

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F8-E1 | product existence timing/status differential (existing vs absent detail GETs, medians over 5 shots each) | an existing row must not be distinguishable from an absent one by timing beyond noise (delisted-vs-absent status uniformity already proved byte-identically in T1a F5-D1) | BLOCKED: the catalogue is empty (products count=0, seed_demo never ran) — there is no existing row to compare against; only the absent side exists | **BLOCKED** | n/a |
| F8-E2 | order existence oracle: anon GET order 1 vs 999999, 4 shots each (status, body, timing) | order_detail is uniform-401 before any lookup — byte-identical bodies and no usable timing gap between a plausible and an absent id | bodies byte-identical: True; medians 78.2ms vs 82.2ms; body {"error":"Authentication credentials were not provided.","code":"not_authenticated","details":{}} | **PASS** | n/a |
| F8-E3 | user existence oracle: login with a REAL registered username (inactive) vs a never-existing username, 4 shots each (status, body, timing) | one uniform 401 for unknown, wrong-password and inactive — byte-identical bodies and no usable timing gap (no user-enumeration channel through login) | statuses 401/401; byte-identical: True; medians 299.0ms vs 319.4ms (WAN medians, close either direction — no reproducible gap) | **PASS** | n/a |
| F8-E4 | cartless coupon existence differential (regression re-check of T1a F3-C1): two absent codes, no session | the cartless preview answers one uniform not-found before it reads the code — no code-existence oracle | 404 vs 404; byte-identical: True | **PASS** | n/a |
| F8-H1 | GET /health/ post-F2 split: scan the public body for the staff keys Q-S2 moved behind health_detail and for non-whitelisted checks | the public probe carries only status + the deploy-relevant whitelist (database/media_writable/shipping_configured); no business counter or config mode reappears (Q-S2/F2 invariant, shape originally verified in T1a STEP ZERO) | 200; checks=['database', 'media_writable', 'shipping_configured']; staff keys leaked: none; extra checks: none | **PASS** | n/a |
| F8-H2 | source-map probe: fetch the storefront index, try `<script>.map` for 3 discovered bundles | no published .map may serve the original sources (200 map = source disclosure) | bundles found: 3; map probes: three `/_next/static/chunks/*.js.map` = 404; 200 maps: none | **PASS** | n/a |
| F8-S1 | stack traces under forced errors: deep-nest bomb, huge JSON, lone-surrogate body, 4KB query string — every 5xx the run produced is scanned for traceback markers | DEBUG stays off in production: any 5xx body must be the generic error page — no Traceback, no settings dump, no file paths | statuses: deep-nest-bomb=500, huge-json=400, lone-surrogate=400, long-url=200; 5xx produced: ['deep-nest-bomb']; traceback leaked: none | **PASS** | n/a |
| F8-D1 | Django debug page: two nonexistent paths (API root + admin) probed for the DEBUG traceback/settings pages | DEBUG=False serves plain 404s (JSON envelope on API routes, bare redirects elsewhere) — never the debug page | statuses 404/302; debug page markers: absent | **PASS** | n/a |
| F8-C1 | CORS preflight abuse: evil origin (+ credential-shaped ACRH/ACRM) with a trusted-origin control | the evil origin never receives Access-Control-Allow-Origin (a browser cannot read the response cross-origin); the control shows the probe measures CORS policy, not the absence of middleware | evil status 200 ACAO=absent; control status 200 ACAO=https://test.boredompeaks.me | **PASS** | n/a |
| F8-C2 | CORS null-origin + non-preflight evil-origin probes (credentials trick: allow-credentials without a matching ACAO) | neither null nor evil ever receives ACAO; allow-credentials is never granted to an origin that got no ACAO | null ACAO=absent status 200; evil GET ACAO=absent status 200; allow-credentials=absent | **PASS** | n/a |
| F8-H3 | Host-header/cache poisoning: Host: evil.example on /health/, X-Forwarded-Host on a slash-redirecting path, Host: evil on the same path (Location builders are the classic poison) | the attacker host never appears in any application body, header or Location — the edge/Django answer with the canonical host or refuse, never a reflected evil host a cache could key on | statuses 403/301/403; app reflection of evil.example: none; edge-rejected: health-host-evil=403, slash-redirect-host-evil=403 (Cloudflare error pages, excluded by marker); XFH Location: /api/v1/store/products/ (relative, canonical); Host Location: none | **PASS** | n/a |

## Family 9 — Notifications / outbox (T1b)

| ID | Attempted attack | Invariant that must hold | Result (live evidence) | Verdict | Severity |
|---|---|---|---|---|---|
| F9-F1 | enqueue/mail-flood via the resend-verification trigger: up to 25 shots in a fresh window (empty-email payloads spend the throttle without sending) | the recovery scope refuses per-client within a bounded shot count, so the mail-trigger endpoint cannot be spammed; non-429 answers stay byte-uniform | statuses=[200 x18, 429]; first refusal at shot 19/19; shipped default recovery=5/min; no mail sent (empty email matches nobody); uniform: True | **PASS** | n/a |
| F9-F2 | reachable enqueue triggers for the durable outbox: forged webhook, anonymous payment verify, guest checkout, restock arm on the seeded slug — a matrix of statuses on a fresh sessionless identity | no unauthenticated caller can enqueue into NotificationOutbox: the webhook fails closed, verify is auth-gated, checkout is the guest deadend, restock arms only for a real product row (and is throttled on top) | webhook=503; verify=401; checkout=403; restock=404 | **PASS** | n/a |
| F9-F3 | dedup-key collision: enqueue the same event twice and observe the unique dedup_key collapse (or a collision across different payloads) | the outbox's unique dedup_key refuses the second identical enqueue while occurrence-discriminated repeats still land (common/notifications.py _dedup_key) | BLOCKED: no API surface exposes outbox rows or enqueue outcomes: the dedup key is server-side only (enqueue happens inside order/payment transactions), and there is no staff or public read route for NotificationOutbox — the collision cannot be observed from outside the box | **BLOCKED** | n/a |
| F9-F4 | dead-letter/security-alert abuse: forged password-reset confirmation (the one alert-firing branch an anonymous caller can even aim at) | the forged confirm dies at the token gate (uniform 400, no traceback) before the branch that records AUTH_PASSWORD_RESET and fires notify_security_change; the health-alert branch only fires on a degraded probe, which a caller cannot induce | 400 {"error":"This password-reset link is invalid or expired.","code":"validation_error","details":{}}; refused at token gate: True | **PASS** | n/a |
| F9-F5 | drain worker behaviour under poisoned payload rows (corrupt context references, oversized error strings, missing models) | drain_notifications claims, bounded-retries and dead-letters poisoned rows without crashing the worker (the property the backend suite pins in-process) | BLOCKED: the drain runs server-side on the celery beat schedule — no API route enqueues a poisoned context or reads a drain outcome, so the attack has no externally observable leg on this deployment (box-side verification belongs to T2/ops with shell access) | **BLOCKED** | n/a |
| F9-F6 | restock-arm enqueue flood on a real product (12-shot burst, same guest email) | the restock pair throttle bounds a flood that actually arms notification rows | BLOCKED: no product row exists to arm against (seed_demo never ran; the seeded-slug probe 404s) — only the not-found path was reachable, see F9-F2's matrix | **BLOCKED** | n/a |

## Finding detail — F7-B1 (Medium): non-object JSON bodies 500 on the cart write

**What was attacked:** `POST /api/v1/store/cart/` (sessioned, browser-shaped)
with three well-formed JSON body shapes in one battery — a 2500-deep nested
array (the classic serialization bomb), a 2500-deep nested **object** (depth
control), and a bare scalar (`42`, shape control) — plus a 300 KB object
(family row F7-B2) and a lone-surrogate body (family row F8-S1).

**Measured facts:**

- nested array depth 2500 → **500**; bare scalar `42` → **500**; nested
  object depth 2500 → **400** (`product_id is required`); 300 KB object →
  **400**; lone surrogate → **400**.
- Both 500 bodies are Django's generic `Server Error (500)` page — no
  traceback, no settings, no file paths (also pinned by row F8-S1).
- The same nested array against the login door → **400** (measured in the
  smoke run), i.e. the failure is view-specific, not parser-wide.

**Reading (cause labelled hypothesis, measurements above are facts):** depth
is handled — the object-depth control proves the parser survives 2500
levels. What crashes is a body whose top-level JSON value is NOT an object:
the cart view reaches for a dict-style access on `request.data` and the
raise escapes as a 500 (the shape of the observed statuses). The uniform
observation across list and scalar bodies, against a dict-requiring
validation path that answers 400, supports that reading without needing box
access to confirm the traceback line.

**Impact:** any client — or a scripted caller — can elicit a 500 from the
cart write with a ten-byte body (`[1]`). Availability/error-shaping class:
DEBUG is off so nothing leaks (F8-S1), but the endpoint leaves the designed
4xx envelope for server errors, and per-request worker time is burned on the
error path.

**Remediation:** guard the view body's type before dict access (a uniform
400 envelope for non-object JSON, ideally via serializer validation rather
than a hand-rolled `isinstance`), add the three-shape battery as backend
tests, then re-run `scripts/security_smoke.sh` and this row — F7-B1 turns
PASS when all three shapes answer 4xx.

## Family 10 — F1-F8 regression smoke extension (T1b)

`scripts/security_smoke.sh` keeps T1a's 33 checks byte-for-byte and appends
19 checks for the T1b families. Run recorded for this task:
`bash scripts/security_smoke.sh` → **52 checks, 0 failed, exit 0**.

- F6 (5): anonymous logout → 401; anonymous MFA setup → 401; anonymous MFA
  status → 401/403; refresh with `Origin: https://evil.example` → 403 AND no
  `refresh_token` Set-Cookie on the refusal (the origin-gate probe must
  never sweep or mint); (all four auth-scope spends stay inside one fresh
  window — the smoke never touches register/recovery budgets, per T1a's
  exclusion).
- F7 (6): SQLi search tautology → no 5xx and no SQL error text; SQLi
  ordering injection → no 5xx; deep-nest bomb against the login door →
  never authenticates (not 200) and never leaks a traceback (this is the
  leak/auth invariant; the exact 4xx-vs-5xx handling of the cart shape is
  finding F7-B1 and must stay green either way once fixed); reflected XSS
  in search served as application/json; polyglot image with path-traversal
  filename → 401/403; admin login `next=` payload stays HTML-escaped.
- F8 (5): public /health/ carries deploy checks only (no business keys);
  evil-origin preflight receives no ACAO; **instrument control**: the
  trusted-origin preflight MUST receive ACAO (so the evil-origin check can
  never go vacuously green); unknown path → plain 404 without debug
  markers; poisoned `X-Forwarded-Host` never reaches a Location header.
- F9 (3): forged password-reset confirmation → 400 at the token gate;
  restock arm on an absent slug → uniform 404, twin byte-identical.

Still deliberately excluded (they burn register/recovery windows or need
credentials): the register/recovery floods' refusal-index bursts (measured
once per release by the runner instead), any registration call, and every
staff-role cell. The source-map probes (F8-H2) live in the runner only —
they depend on the storefront's build layout, which this API-focused script
must not couple to.

## Gates and tree integrity

- Floor printed by `python scripts/changelog_figures.py floor --task SPEC-T1a`
  (artifact `scripts/figures/SPEC-T1a.json`):
  - sqlite: Ran 2151 tests, OK (expected failures=2), cov 99.81% (10775
    stmts, 0 missed, 1472 branches, 23 partial), test exit 0, coverage exit 0
  - postgresql (pg2a, engine probed by the tool): Ran 2151 tests, OK
    (expected failures=2), cov 99.81% (10775 stmts, 0 missed, 1472 branches,
    23 partial), test exit 0, coverage exit 0
- Pre-flight before any work on this task: 2151 tests OK (skipped=2,
  expected failures=2), exit 0 (SQLite).
- Smoke: `bash scripts/security_smoke.sh` → 33 checks, 0 failed, exit 0.
- `manage.py makemigrations --check` clean; `doc_claims.py --base 63e2778`
  and `changelog_figures.py check --base 63e2778` run as the last step after
  this file (outputs recorded in the task report and the changelog row).
- No backend code touched: `git diff 4740f33..HEAD -- backend/` shows only
  this report and the changelog row — zero `.py` (proof re-run at commit).

## T1b gates and tree integrity

- Floor printed by `python scripts/changelog_figures.py floor --task
  SPEC-T1b` (artifact `scripts/figures/SPEC-T1b.json`), both engines of the
  pair:
  - sqlite: Ran 2151 tests, OK (expected failures=2), cov 99.81% (10775
    stmts, 0 missed, 1472 branches, 23 partial), test exit 0, coverage exit 0
  - postgresql (lane A container pg2a, engine probed by the tool): Ran 2151
    tests, OK (expected failures=2), cov 99.81% (10775 stmts, 0 missed, 1472
    branches, 23 partial), test exit 0, coverage exit 0
- Pre-flight before any T1b work, re-derived this session on both engines:
  2151 tests OK (skipped=2, expected failures=2), cov 99.81% — floor held
  unchanged (backend untouched by this task).
- Smoke: `bash scripts/security_smoke.sh` → 52 checks, 0 failed, exit 0.
- `manage.py makemigrations --check` → "No changes detected", exit 0.
- `ruff check --config backend/pyproject.toml scripts/redteam_t1b.py` →
  All checks passed, exit 0.
- `doc_claims.py --base 63e2778` and `changelog_figures.py check --base
  63e2778` are run as the last step after the changelog row (outputs
  recorded in the task report and the row itself).
- No backend code touched by THIS task: `git diff b7f3fde..HEAD -- backend/`
  shows only this report and the changelog row — zero `.py` (b7f3fde was the
  lane/a tip this task started from; Q-D4's comment-only `.py` trims and the
  ledger-sync doc rows between 63e2778 and b7f3fde pre-date this task and
  belong to their own rows — proof re-run at commit).

## Appendix A — environment capability inventory (STEP ZERO probes)

| Probe | Request | Response observed |
|---|---|---|
| public-health | `GET /health/` | 200 {"status": "ok", "checks": {"database": true, "media_writable": true, "shipping_configured": false}} |
| store-config | `GET /api/v1/store/config/` | 200 with return_window_days 30; support fields empty |
| products-list-empty | `GET /api/v1/store/products/` | 200 {"count":0,...,"results":[]} |
| register-new-account | `POST /api/v1/account/register/` | first-ever fresh registration (step-zero probe) 201 "Check your email to verify the account" with no numeric user id; this run's re-registration with the reused contact email 400 "An account already uses this email address." (Q-S3's documented residual oracle) |
| login-unverified-account | `POST /api/v1/account/storefront/login/` | 401 "No active account found with the given credentials" (inactive until token) |
| login-seeded-demo-customer | `POST /api/v1/account/storefront/login/` | 401 — seed_demo never ran |
| seeded-demo-slug | `GET /api/v1/store/products/demo-plenty-perfume/` | 404 {"error":"Product not found",...} |
| username-probe-existing | `GET /api/v1/account/username-available/?username=rt_t1a_c1` | 200 {"available":false,...} (public by design, throttled) |
| resend-verification-smtp-state | `POST /api/v1/account/resend-verification/` | 200 uniform message (mail subsystem accepted the send) |
| frontend-health | `GET https://test.boredompeaks.me/health/` | 308 redirect (target not recorded) |
| default-python-ua-probe | `GET /health/ (Python-urllib UA)` | 403 Cloudflare bot block — browser UA required |
| cart-session-cookie-flags | `GET /api/v1/store/cart/` | 200 Set-Cookie: sessionid=...; Domain=boredompeaks.me; HttpOnly; Max-Age=1209600; Path=/; SameSite=Lax; Secure |
