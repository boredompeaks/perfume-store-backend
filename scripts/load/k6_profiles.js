// SPEC-T2a — k6 load profiles for the Phase T2 "UAT box limits" program.
//
// Authored at T2-a time; EXECUTION happens at T2 (see backend/docs/run/
// T2-plan.md for the playbook, knee rubric and runbook text). Zero live
// traffic was sent while authoring this file.
//
// ONE file, three profile families, selected with K6_SCENARIO:
//   browse_cart     — read-heavy catalog browse + session-cart ops
//   checkout_ramp   — staged VU ramp of authenticated checkout (internal
//                     latency) plus a SEPARATE, hard-capped payment-intent
//                     scenario (the only traffic that reaches Razorpay's
//                     test API — the cap keeps gateway volume bounded and
//                     keeps gateway latency separable from internal latency)
//   race_suite      — HTTP-level legs of the money-invariant races; the
//                     AUTHORITATIVE capture-level runners (local HMAC
//                     signature minting + post-run DB invariant checks) are
//                     scripts/load/run_races.py — this file's race legs and
//                     their DB-INV markers mirror that vocabulary
//
// Target base URL comes from LOAD_BASE_URL (required — no hardcoded host).
// Cloudflare blocks the default python UA on this deployment (measured in
// the T1a red-team run); a browser-shaped UA is therefore sent explicitly
// and can be overridden with LOAD_USER_AGENT.
//
// Throttle budget awareness (config/settings.py DEFAULT_THROTTLE_RATES):
// auth 10/min, cart 60/min, coupon 10/min, payment 10/min — scopes are
// per-client (user when authenticated, ident/IP otherwise). Profiles are
// sized under these budgets; a 429 wall is a RECORDED outcome, not an
// outage, and the knee rubric in T2-plan.md reads the two apart.
//
// Static validation status: no k6 binary was on the authoring machine
// (Get-Command k6 -> not found), so this file is SYNTAX-PARSE-validated
// only (node --input-type=module --check) plus the local stage-math sanity
// harness described in the T2-a report. Never treat that as a k6 run.

import http from 'k6/http';
import { check, group, sleep, fail } from 'k6';

// ---------------------------------------------------------------------------
// Environment-driven configuration (no secrets, no hosts in this file).
// ---------------------------------------------------------------------------

function intEnv(name, dflt) {
  const raw = __ENV[name];
  if (raw === undefined || raw === '') return dflt;
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) throw new Error(name + ' must be an integer, got ' + raw);
  return n;
}

function numEnv(name, dflt) {
  const raw = __ENV[name];
  if (raw === undefined || raw === '') return dflt;
  const n = parseFloat(raw);
  if (Number.isNaN(n)) throw new Error(name + ' must be a number, got ' + raw);
  return n;
}

export const CONFIG = {
  baseUrl: (__ENV.LOAD_BASE_URL || '').replace(/\/+$/, ''),
  // Origin/Referer for sessioned (cart) POSTs: Django's CSRF referer check
  // must see a trusted same-origin or CSRF_TRUSTED_ORIGINS value. Defaults
  // to the API origin itself; override when the API is reached through the
  // storefront origin the CSRF allowlist names.
  trustedOrigin: (__ENV.LOAD_TRUSTED_ORIGIN || __ENV.LOAD_BASE_URL || '').replace(
    /\/+$/,
    ''
  ),
  userAgent:
    __ENV.LOAD_USER_AGENT ||
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  // Razorpay intent-creation HARD CEILING for this whole file: the
  // intents_capped scenario's VUs x iterations may never exceed it, and
  // setup() refuses to start if an override pushes the math past it.
  intentCap: intEnv('LOAD_MAX_INTENTS', 20),
  // Race sizing: oversell/coupon legs fire one simultaneous checkout per
  // buyer; capture-replay leg fires LOAD_CAPTURE_REPLAYS parallel verifies
  // as ONE user (payment scope is 10/min per user — keep <= 8).
  raceN: intEnv('LOAD_RACE_N', 8),
  captureReplays: intEnv('LOAD_CAPTURE_REPLAYS', 6),
  // Knee-detection thresholds (env-tunable so the rubric can be re-run at
  // different strictness without editing this file).
  p95InternalMs: numEnv('LOAD_P95_INTERNAL_MS', 500),
  p95RazorpayMs: numEnv('LOAD_P95_RAZORPAY_MS', 2500),
  errRateMax: numEnv('LOAD_ERROR_RATE_MAX', 0.01),
  buyerLogins: [],
  productSlugs: [],
  productIds: {},
  couponCode: __ENV.LOAD_COUPON_CODE || '',
  // Pre-minted capture triple for the double-capture REPLAY leg. Mint it
  // server-side without exposing the secret:
  //   backend\venv\Scripts\python scripts\load\run_races.py mint-signature \
  //     --razorpay-order-id <order_XYZ> --payment-id payLOADrun0001
  capture: {
    rzpOrderId: __ENV.LOAD_CAPTURE_RZP_ORDER_ID || '',
    paymentId: __ENV.LOAD_CAPTURE_PAYMENT_ID || '',
    signature: __ENV.LOAD_CAPTURE_SIGNATURE || '',
  },
};

// Which named scenarios each K6_SCENARIO family runs. Race legs are
// start-time staggered so their shared auth-throttle windows (10/min
// anonymous logins, 10/min payment verifies) do not collide.
const SELECT = {
  browse_cart: ['browse_reads', 'browse_cart_ops'],
  checkout_ramp: ['checkout_ramp', 'intents_capped'],
  race_suite: ['race_oversell_checkout', 'race_coupon_checkout', 'race_capture_replay'],
};

// intents_capped: VUs x iterations <= CONFIG.intentCap, by construction.
const INTENT_VUS = Math.min(4, CONFIG.intentCap);
const INTENT_ITERS = Math.max(1, Math.floor(CONFIG.intentCap / INTENT_VUS));

// ---------------------------------------------------------------------------
// Scenario definitions (plain data — the local stage-math harness reads
// these without running k6).
// ---------------------------------------------------------------------------

export const SCENARIOS = {
  // Read-heavy catalog + cart browse. Two executors: pure reads at a
  // constant arrival rate, session-cart mutations at a lower rate (each
  // mutation spends from the 60/min 'cart' budget of one session).
  browse_reads: {
    executor: 'constant-arrival-rate',
    rate: intEnv('LOAD_BROWSE_RPS', 20),
    duration: __ENV.LOAD_BROWSE_DURATION || '2m',
    preAllocatedVUs: 30,
    maxVUs: 60,
    exec: 'browseReads',
    thresholds: {
      http_req_duration: ['p(95)<' + CONFIG.p95InternalMs],
      http_req_failed: ['rate<' + CONFIG.errRateMax],
      checks: ['rate>0.99'],
    },
  },
  browse_cart_ops: {
    executor: 'constant-arrival-rate',
    rate: intEnv('LOAD_CART_OPS_RPS', 2),
    duration: __ENV.LOAD_BROWSE_DURATION || '2m',
    preAllocatedVUs: 8,
    maxVUs: 16,
    exec: 'browseCartOps',
    thresholds: {
      http_req_duration: ['p(95)<' + CONFIG.p95InternalMs],
      // Cart ops are session+CSRF mutations; 4xx here means the CSRF or
      // throttle contract broke, which is a finding, not noise.
      http_req_failed: ['rate<' + CONFIG.errRateMax],
    },
  },
  // Staged checkout ramp: monotonic VU ramp with hold bands, then ramp
  // down. Internal latency only — no payment intents in this executor.
  checkout_ramp: {
    executor: 'ramping-vus',
    stages: [
      { target: intEnv('LOAD_RAMP_VUS', 5), duration: '1m' },
      { target: intEnv('LOAD_RAMP_VUS', 5), duration: '2m' },
      { target: intEnv('LOAD_RAMP_VUS_MAX', 12), duration: '2m' },
      { target: intEnv('LOAD_RAMP_VUS_MAX', 12), duration: '3m' },
      { target: 0, duration: '30s' },
    ],
    exec: 'checkoutRamp',
    thresholds: {
      'http_req_duration{svc:internal}': ['p(95)<' + CONFIG.p95InternalMs],
      http_req_failed: ['rate<' + CONFIG.errRateMax],
    },
  },
  // The ONLY scenario that creates Razorpay intents. per-vu-iterations so
  // the total intent volume is exactly VUs x iterations and that product is
  // bounded by CONFIG.intentCap in setup() (hard ceiling, not a hope).
  // Each iteration = one fresh checkout + one create_payment call; the
  // gateway round trip is tagged svc:razorpay so its latency separates
  // from the internal checkout latency in the same iteration.
  intents_capped: {
    executor: 'per-vu-iterations',
    vus: INTENT_VUS,
    iterations: INTENT_ITERS,
    maxDuration: '5m',
    startTime: '11m30s', // after the checkout_ramp stages above
    exec: 'paymentIntent',
    thresholds: {
      'http_req_duration{svc:internal}': ['p(95)<' + CONFIG.p95InternalMs],
      'http_req_duration{svc:razorpay}': ['p(95)<' + CONFIG.p95RazorpayMs],
      http_req_failed: ['rate<' + CONFIG.errRateMax],
    },
  },
  // Race leg 1 — oversell of the last unit at CHECKOUT level: raceN
  // distinct buyers fire one checkout each at the same startTime.
  // Capture-level authority (local-HMAC verify race + DB invariant checks)
  // is run_races.py; marker T2-INV-1..3 below names what its post-run DB
  // check asserts.
  race_oversell_checkout: {
    executor: 'per-vu-iterations',
    vus: CONFIG.raceN,
    iterations: 1,
    startTime: '0s',
    exec: 'raceOversellCheckout',
    thresholds: {
      checks: ['rate==1'],
      http_req_failed: ['rate<0.5'],
    },
  },
  // Race leg 2 — single-use coupon double-burn at CHECKOUT level (same
  // shape, coupon in the payload). Capture-level authority and the
  // used_count-once DB check (T2-INV-5) live in run_races.py.
  race_coupon_checkout: {
    executor: 'per-vu-iterations',
    vus: CONFIG.raceN,
    iterations: 1,
    startTime: '90s', // past the shared anonymous-login window of leg 1
    exec: 'raceCouponCheckout',
    thresholds: {
      checks: ['rate==1'],
      http_req_failed: ['rate<0.5'],
    },
  },
  // Race leg 3 — double-capture REPLAY at HTTP level: captureReplays
  // parallel verifies of ONE already-minted (order, payment, signature)
  // triple supplied via env (minted by run_races.py so the key secret
  // never enters a k6 command line). Expects exactly one capture to land;
  // replays no-op (T2-INV-6..7).
  race_capture_replay: {
    executor: 'per-vu-iterations',
    vus: CONFIG.captureReplays,
    iterations: 1,
    startTime: '180s', // past the payment-scope window of any prior leg
    exec: 'raceCaptureReplay',
    thresholds: {
      checks: ['rate==1'],
    },
  },
};

const selected = __ENV.K6_SCENARIO || 'browse_cart';
if (!SELECT[selected]) {
  fail(
    'K6_SCENARIO must be one of ' +
      Object.keys(SELECT).join(', ') +
      " (got '" +
      selected +
      "')"
  );
}

export const options = { scenarios: {} };
for (const name of SELECT[selected]) {
  options.scenarios[name] = SCENARIOS[name];
}

// ---------------------------------------------------------------------------
// Shared helpers.
// ---------------------------------------------------------------------------

function base() {
  if (!CONFIG.baseUrl) {
    throw new Error('LOAD_BASE_URL is required (no hardcoded host in this file)');
  }
  return CONFIG.baseUrl;
}

function browserHeaders(extra) {
  const h = {
    'User-Agent': CONFIG.userAgent,
    Accept: 'application/json',
  };
  if (extra) {
    for (const k of Object.keys(extra)) h[k] = extra[k];
  }
  return h;
}

function jsonHeaders(access, csrf) {
  const h = browserHeaders({ 'Content-Type': 'application/json' });
  if (access) h['Authorization'] = 'Bearer ' + access;
  // Sessioned unsafe methods ride the double-submit CSRF pair, browser
  // shaped (Origin + Referer + X-CSRFToken) — the exact shape the T1a run
  // proved passes the SPEC-17-03 gate.
  if (csrf) {
    h['X-CSRFToken'] = csrf;
    h['Origin'] = CONFIG.trustedOrigin;
    h['Referer'] = CONFIG.trustedOrigin + '/';
  }
  return h;
}

function cookieValue(res, name) {
  if (!res.cookies || !res.cookies[name] || !res.cookies[name].length) {
    return '';
  }
  return res.cookies[name][0].value;
}

function internalTags() {
  return { svc: 'internal' };
}

function razorpayTags() {
  return { svc: 'razorpay' };
}

// Per-VU credential vault: each VU logs in ONCE as its assigned buyer and
// reuses the access token (15-minute lifetime; ramp bands are shorter).
let vault = null;

function assignedBuyer() {
  const logins = CONFIG.buyerLogins;
  if (!logins.length) {
    throw new Error('setup() did not populate LOAD_BUYER_LOGINS');
  }
  // __VU is 1-based; distinct concurrent VUs get distinct buyers so the
  // same-owner pending-order dedup guard can never collapse two racers.
  return logins[(__VU - 1) % logins.length];
}

function ensureLogin() {
  if (vault && vault.expiresAt > Date.now()) {
    return vault;
  }
  const buyer = assignedBuyer();
  const res = http.post(
    base() + '/api/v1/account/storefront/login/',
    JSON.stringify({ username: buyer.username, password: buyer.password }),
    { headers: jsonHeaders(), tags: internalTags() }
  );
  if (res.status !== 200) {
    fail(
      'storefront login failed for ' +
        buyer.username +
        ': HTTP ' +
        res.status +
        ' (auth throttle is 10/min per ident — pace the buyer set or wait out the window)'
    );
  }
  const access = res.json('access');
  // Cookie jar keeps sessionid + csrftoken for the cart half of the flow.
  const csrf = cookieValue(res, 'csrftoken');
  vault = { access: access, csrf: csrf, expiresAt: Date.now() + 12 * 60 * 1000 };
  return vault;
}

function pickedSlug() {
  const slugs = CONFIG.productSlugs;
  if (!slugs.length) throw new Error('setup() did not populate LOAD_PRODUCT_SLUGS');
  return slugs[(__VU - 1) % slugs.length];
}

function productIdFor(slug) {
  if (CONFIG.productIds[slug]) return CONFIG.productIds[slug];
  const res = http.get(base() + '/api/v1/store/products/' + slug + '/', {
    headers: browserHeaders(),
    tags: internalTags(),
  });
  if (res.status !== 200) {
    fail('product detail ' + slug + ' -> HTTP ' + res.status + ' (seeded catalogue required)');
  }
  const id = res.json('id');
  CONFIG.productIds[slug] = id;
  return id;
}

function addToCart(v, slug, quantity) {
  const res = http.post(
    base() + '/api/v1/store/cart/',
    JSON.stringify({ product_id: productIdFor(slug), quantity: quantity }),
    { headers: jsonHeaders(v.access, v.csrf), tags: internalTags() }
  );
  check(res, { 'cart add 2xx': (r) => r.status >= 200 && r.status < 300 });
  return res;
}

function checkoutPayload(couponCode) {
  const payload = {
    full_name: 'Load Ramp Buyer',
    phone: '9876543210',
    address: '12 Load Test Lane',
    city: 'Mumbai',
    state: 'Maharashtra',
    pincode: '400001',
  };
  if (couponCode) payload['coupon_code'] = couponCode;
  return JSON.stringify(payload);
}

function checkout(v, couponCode) {
  // JWT bearer auth; the 'cart' scope (60/min) meters this endpoint per
  // user. svc:internal tag = this leg never leaves the box.
  return http.post(
    base() + '/api/v1/store/orders/checkout/',
    checkoutPayload(couponCode),
    { headers: jsonHeaders(v.access), tags: internalTags() }
  );
}

// ---------------------------------------------------------------------------
// setup(): validate the environment once, populate shared config, and
// enforce the intent-cap arithmetic BEFORE any VU runs.
// ---------------------------------------------------------------------------

export function setup() {
  base(); // throws when LOAD_BASE_URL is missing

  const loginsRaw = __ENV.LOAD_BUYER_LOGINS || '';
  if (loginsRaw) {
    let parsed;
    try {
      parsed = JSON.parse(loginsRaw);
    } catch (e) {
      throw new Error('LOAD_BUYER_LOGINS must be a JSON array of {username,password}');
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
      throw new Error('LOAD_BUYER_LOGINS must be a non-empty JSON array');
    }
    CONFIG.buyerLogins = parsed;
  }

  const slugsRaw = __ENV.LOAD_PRODUCT_SLUGS || '';
  CONFIG.productSlugs = slugsRaw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);

  // Hard ceiling on Razorpay intent volume: the intents scenario product
  // must sit at or under CONFIG.intentCap, whatever the env overrides say.
  const intentTotal = INTENT_VUS * INTENT_ITERS;
  if (intentTotal > CONFIG.intentCap) {
    throw new Error(
      'intent scenario math (' +
        INTENT_VUS +
        ' VUs x ' +
        INTENT_ITERS +
        ' iterations = ' +
        intentTotal +
        ') exceeds LOAD_MAX_INTENTS=' +
        CONFIG.intentCap
    );
  }

  const scenarios = Object.keys(options.scenarios);
  if (scenarios.indexOf('checkout_ramp') !== -1) {
    const rampMax = intEnv('LOAD_RAMP_VUS_MAX', 12);
    if (CONFIG.buyerLogins.length < rampMax) {
      throw new Error(
        'checkout_ramp needs >= LOAD_RAMP_VUS_MAX distinct buyers (have ' +
          CONFIG.buyerLogins.length +
          ', need ' +
          rampMax +
          ') — same-owner checkouts can collapse in the pending-order dedup guard'
      );
    }
  }
  if (scenarios.indexOf('intents_capped') !== -1) {
    if (CONFIG.buyerLogins.length < INTENT_VUS) {
      throw new Error(
        'intents_capped needs >= ' + INTENT_VUS + ' distinct buyers (have ' +
          CONFIG.buyerLogins.length + ')'
      );
    }
  }
  if (scenarios.indexOf('browse_reads') !== -1 && CONFIG.productSlugs.length === 0) {
    throw new Error('browse_cart needs LOAD_PRODUCT_SLUGS (comma-separated seeded slugs)');
  }
  if (scenarios.indexOf('browse_cart_ops') !== -1 && CONFIG.buyerLogins.length === 0) {
    throw new Error('browse_cart_ops needs LOAD_BUYER_LOGINS (session+CSRF mutations)');
  }
  if (SELECT[selected].indexOf('race_oversell_checkout') !== -1) {
    if (CONFIG.buyerLogins.length < CONFIG.raceN) {
      throw new Error(
        'race_suite needs >= LOAD_RACE_N distinct buyers (have ' +
          CONFIG.buyerLogins.length +
          ', need ' +
          CONFIG.raceN +
          ')'
      );
    }
    if (!CONFIG.productSlugs.length) {
      throw new Error('race_suite needs LOAD_PRODUCT_SLUGS naming the last-unit product');
    }
    if (!CONFIG.couponCode) {
      throw new Error('race_suite needs LOAD_COUPON_CODE (the usage_limit=1 coupon)');
    }
  }
  if (
    SELECT[selected].indexOf('race_capture_replay') !== -1 &&
    (!CONFIG.capture.rzpOrderId || !CONFIG.capture.paymentId || !CONFIG.capture.signature)
  ) {
    throw new Error(
      'race_capture_replay needs the pre-minted triple: LOAD_CAPTURE_RZP_ORDER_ID, ' +
        'LOAD_CAPTURE_PAYMENT_ID, LOAD_CAPTURE_SIGNATURE (mint with run_races.py ' +
        'mint-signature so RAZORPAY_KEY_SECRET never appears on a k6 command line)'
    );
  }

  // Eager product-id resolution once (setup runs in the init VU; the ids
  // become part of returned data and are copied into CONFIG per VU below).
  const ids = {};
  for (const slug of CONFIG.productSlugs) {
    const res = http.get(base() + '/api/v1/store/products/' + slug + '/', {
      headers: browserHeaders(),
      tags: internalTags(),
    });
    if (res.status !== 200) {
      throw new Error('product ' + slug + ' -> HTTP ' + res.status + ' (run seed first)');
    }
    ids[slug] = res.json('id');
  }
  CONFIG.productIds = ids;

  return {
    selected: selected,
    productIds: ids,
    intentScenario: {
      vus: INTENT_VUS,
      iterations: INTENT_ITERS,
      total: intentTotal,
      cap: CONFIG.intentCap,
    },
  };
}

// ---------------------------------------------------------------------------
// Executed scenarios.
// ---------------------------------------------------------------------------

// Read-heavy catalog browse: list -> detail -> config -> cart read.
export function browseReads() {
  const slug = pickedSlug();
  const list = http.get(base() + '/api/v1/store/products/', {
    headers: browserHeaders(),
    tags: internalTags(),
  });
  check(list, { 'product list 200': (r) => r.status === 200 });

  const detail = http.get(base() + '/api/v1/store/products/' + slug + '/', {
    headers: browserHeaders(),
    tags: internalTags(),
  });
  check(detail, { 'product detail 200': (r) => r.status === 200 });

  const cfg = http.get(base() + '/api/v1/store/config/', {
    headers: browserHeaders(),
    tags: internalTags(),
  });
  check(cfg, { 'store config 200': (r) => r.status === 200 });

  // Session-keyed cart read mints/keeps a cart session; GETs bypass the
  // cart mutation throttle by design.
  const cart = http.get(base() + '/api/v1/store/cart/', {
    headers: browserHeaders(),
    tags: internalTags(),
  });
  check(cart, { 'cart read 200': (r) => r.status === 200 });
}

// Session-cart mutations, browser-shaped (CSRF double-submit). One add ->
// patch -> delete cycle per iteration against the VU's own cart session.
export function browseCartOps() {
  const v = ensureLogin();
  const slug = pickedSlug();
  group('cart ops', function () {
    const add = addToCart(v, slug, 1);
    const itemId = add.status >= 200 && add.status < 300 ? add.json('id') : null;
    if (itemId === null) return; // check inside addToCart already recorded it

    const patch = http.patch(
      base() + '/api/v1/store/cart/' + itemId + '/',
      JSON.stringify({ quantity: 2 }),
      { headers: jsonHeaders(v.access, v.csrf), tags: internalTags() }
    );
    check(patch, { 'cart patch 2xx': (r) => r.status >= 200 && r.status < 300 });

    const del = http.del(base() + '/api/v1/store/cart/' + itemId + '/', null, {
      headers: jsonHeaders(v.access, v.csrf),
      tags: internalTags(),
    });
    check(del, { 'cart delete 2xx': (r) => r.status >= 200 && r.status < 300 });
  });
  sleep(1);
}

// Authenticated checkout ramp — INTERNAL latency only. Every iteration
// spends one pending order + one reservation against the seeded stock; the
// T2-plan cleanup step re-runs seed_demo --cleanup between profile runs.
export function checkoutRamp() {
  const v = ensureLogin();
  const slug = pickedSlug();
  addToCart(v, slug, 1);
  const res = checkout(v, null);
  check(res, {
    'checkout created (201/200)': (r) => r.status === 201 || r.status === 200,
    // A 429 here is the cart throttle budget, not a failure of the box;
    // the knee rubric reads throttles separately from 5xx/timeouts.
    'checkout not throttled': (r) => r.status !== 429,
  });
  sleep(numEnv('LOAD_RAMP_SLEEP_S', 5));
}

// The capped Razorpay-intent scenario: one fresh checkout + one
// create_payment per iteration, gateway latency tagged svc:razorpay so the
// internal/gateway split is a threshold away, not a post-hoc guess.
export function paymentIntent() {
  const v = ensureLogin();
  const slug = pickedSlug();
  addToCart(v, slug, 1);
  const orderRes = checkout(v, null);
  if (orderRes.status !== 201 && orderRes.status !== 200) {
    fail('checkout before intent failed: HTTP ' + orderRes.status);
  }
  const orderId = orderRes.json('id');

  const intent = http.post(
    base() + '/api/v1/store/orders/payment/',
    JSON.stringify({ order_id: orderId }),
    { headers: jsonHeaders(v.access), tags: razorpayTags() }
  );
  check(intent, {
    'intent minted 2xx': (r) => r.status >= 200 && r.status < 300,
    'intent not throttled': (r) => r.status !== 429,
  });
  sleep(numEnv('LOAD_INTENT_SLEEP_S', 3));
}

// Race leg 1 — N simultaneous checkouts of the last-unit product.
// HTTP-level leg only; the capture race and the DB invariant checks are
// scripts/load/run_races.py. DB markers asserted post-run there:
//   T2-INV-1 no negative stock            (products.stock >= 0)
//   T2-INV-2 exactly one captured winner  (payment_status='captured' == 1)
//   T2-INV-3 one SALE ledger row set per captured order, none for losers
export function raceOversellCheckout() {
  const v = ensureLogin();
  const slug = pickedSlug();
  addToCart(v, slug, 1);
  const res = checkout(v, null);
  check(res, {
    'race checkout answered': (r) => r.status >= 200 && r.status < 500,
    'race checkout not a server error': (r) => r.status < 500,
  });
  console.log(
    '[T2-INV marker] oversell leg done by VU ' +
      __VU +
      ': HTTP ' +
      res.status +
      ' — run_races.py asserts T2-INV-1/2/3 post-run'
  );
}

// Race leg 2 — N simultaneous checkouts carrying the single-use coupon.
// Post-run DB markers (run_races.py): T2-INV-1..3 plus
//   T2-INV-5 coupon used_count == 1 and at most one captured order holds it
export function raceCouponCheckout() {
  const v = ensureLogin();
  const slug = pickedSlug();
  addToCart(v, slug, 1);
  const res = checkout(v, CONFIG.couponCode);
  check(res, {
    'coupon race checkout answered': (r) => r.status >= 200 && r.status < 500,
  });
  console.log(
    '[T2-INV marker] coupon leg done by VU ' +
      __VU +
      ': HTTP ' +
      res.status +
      ' — run_races.py asserts T2-INV-5 post-run'
  );
}

// Race leg 3 — parallel REPLAY of one pre-minted capture triple. The
// signature was minted server-side (HMAC-SHA256 over
// "<razorpay_order_id>|<payment_id>" with the gateway test secret); k6
// never sees the secret. Post-run DB markers (run_races.py):
//   T2-INV-6 order captured exactly once (payment_status + one transition)
//   T2-INV-7 SALE ledger rows for the order == item count (replays no-op)
export function raceCaptureReplay() {
  ensureLogin(); // session identity; verify itself rides the JWT
  const res = http.post(
    base() + '/api/v1/store/orders/payment/verify/',
    JSON.stringify({
      razorpay_order_id: CONFIG.capture.rzpOrderId,
      razorpay_payment_id: CONFIG.capture.paymentId,
      razorpay_signature: CONFIG.capture.signature,
    }),
    { headers: jsonHeaders(vault.access), tags: internalTags() }
  );
  check(res, {
    'capture replay answered': (r) => r.status >= 200 && r.status < 500,
  });
  console.log(
    '[T2-INV marker] capture replay by VU ' +
      __VU +
      ': HTTP ' +
      res.status +
      ' — run_races.py asserts T2-INV-6/7 post-run'
  );
}

export default function () {
  // Each executor function above is selected by its scenario's exec field;
  // this default is only k6's required export and must never be reached
  // when options.scenarios is populated (it always is, above).
  fail('no scenario exec selected');
}
