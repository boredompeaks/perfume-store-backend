# SPEC-T1a — Red-team attack catalog, first half (families 1-5)

Task SPEC-T1a on lane A, owner charter: **"break the hardening."**
Deliverable of BUILD-QUEUE Phase T, T1 items 1-5. Every row below is one
attack run LIVE from this sandbox over HTTPS against the real test
deployment (`api.boredompeaks.me`, storefront `test.boredompeaks.me`), test
mode only, no real money anywhere, webhook/intent volume capped, UAT OOM
accepted as informative. Runner: `scripts/redteam_t1a.py` (stdlib urllib, no
new dependencies); regression subset: `scripts/security_smoke.sh`.

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
| **Total** | **33** | **1** | **24** | **58** |

One finding: **F3-C4 (Medium)** — the coupon scope's shipped default budget
was not enforced per client at 12 shots; controls localize it as
deployment-shaped, not view-shaped (detail below). The 24 BLOCKED rows are
not defects: they need state/config the box does not have (deployment
section). All executable money-path, uniformity, session and RBAC attacks
held.

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
and every staff-role cell. T1b appends the families 6-10 subset to this same
script.

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
