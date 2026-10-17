#!/usr/bin/env python
"""SPEC-T2a — concurrent money-invariant race runner for Phase T2.

Executes the real-parallel HTTP races (stdlib urllib + threads; NO k6
dependency, NO new packages) and then evaluates the post-run DB invariant
checks the Phase T2 queue names: ledger consistency, no negative stock,
exactly-once capture. Celery-free. This file is the AUTHORITATIVE capture-
level racer; the k6 file's race legs are HTTP-level mirrors that print the
same T2-INV markers.

The races (each mirrors a T1a BLOCKED row that needs live deployment state):

  oversell       N distinct buyers each hold a cart line for the last unit
                 of LOAD_PRODUCT_SLUG; all fire checkout simultaneously, all
                 winners mint an intent, then all captures (payment/verify)
                 fire simultaneously. Exactly one capture may win.
  coupon         Same shape, every checkout carries LOAD_COUPON_CODE (a
                 usage_limit=1 coupon). The single-use burn must land once.
  double-capture ONE order, ONE minted intent, M parallel verifies replaying
                 the SAME (razorpay_order_id, payment_id, signature) triple.
                 Capture must move stock/money exactly once.

The Razorpay signature is minted LOCALLY with hmac over
"<razorpay_order_id>|<payment_id>" using RAZORPAY_KEY_SECRET — the same
computation the gateway SDK performs — so no call to Razorpay's API is made
by this script beyond the bounded intent mints (create_payment). The secret
is read from the environment and never printed, logged, or committed.

DB invariant checks — TWO honest modes (pick one; the runner refuses to
claim "invariants pass" without one of them):

  A. live-DB mode      set LOAD_PG_DSN (preferred) or DATABASE_URL to a
                       postgres DSN and have psycopg importable (it already
                       is inside backend/venv). The runner connects AFTER
                       the HTTP waves and evaluates every check itself.
  B. server-side       `--emit-sql out.json` writes the exact parameterized-
     round-trip        as-literal SQL + expectations for this run's ids;
                       the operator runs those queries on the box (psql /
                       manage.py dbshell), captures the rows as JSON, and
                       the runner evaluates them with `--verify-json
                       rows.json`. Nothing but row data crosses the airgap —
                       the runner never needs DB credentials in this mode.

Credentials: NEVER pass them as arguments and never commit them. The runner
reads LOAD_PG_DSN / DATABASE_URL / RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET /
LOAD_BUYER_LOGINS from the environment (see backend/docs/run/T2-plan.md).

Usage:
  backend\\venv\\Scripts\\python scripts\\load\\run_races.py run \\
      --phase all|oversell|coupon|double-capture [options]
  backend\\venv\\Scripts\\python scripts\\load\\run_races.py mint-signature \\
      --razorpay-order-id order_XXX --payment-id payLOADrun0001

Exit codes (run): 0 = HTTP phase complete AND every DB invariant holds (or
--skip-db was given EXPLICITLY, in which case results are stamped
UNVERIFIED); 1 = at least one DB invariant violated; 2 = setup/preflight
error (nothing was raced); 3 = HTTP phase ran but invariants could not be
evaluated (no DB mode available).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants (endpoints verified against config/urls.py + app urlconfs on
# lane/b @32f0467; behaviour facts from the T1a red-team report).
# ---------------------------------------------------------------------------

LOGIN_PATH = "/api/v1/account/storefront/login/"
CART_PATH = "/api/v1/store/cart/"
PRODUCT_DETAIL = "/api/v1/store/products/{slug}/"
CHECKOUT_PATH = "/api/v1/store/orders/checkout/"
PAYMENT_PATH = "/api/v1/store/orders/payment/"
VERIFY_PATH = "/api/v1/store/orders/payment/verify/"
HEALTH_PATH = "/health/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Shipping payload the checkout view requires (orders/views/checkout.py
# required_fields / _SHIPPING_FIELDS, mirrored by common/testing.py).
CHECKOUT_SHIPPING = {
    "full_name": "Race Buyer",
    "phone": "9876543210",
    "address": "9 Race Lane",
    "city": "Mumbai",
    "state": "Maharashtra",
    "pincode": "400001",
}

LOGIN_PACE_S = float(os.getenv("LOAD_LOGIN_PACE_S", "7"))
# 'auth' throttle is 10/min per ident and anonymous logins key on the
# client ident: pace logins across ALL threads so the wave never starts
# with a buyer stuck at 429.
# Default aligned with k6_profiles.js LOAD_MAX_INTENTS (both 20): one
# ceiling value, two files, no silent divergence (auditor BUG-4).
INTENT_CAP = int(os.getenv("LOAD_MAX_INTENTS", "20"))
# 'payment' scope is 10/min per user — the double-capture replay width
# stays under it by construction.
CAPTURE_REPLAYS = int(os.getenv("LOAD_CAPTURE_REPLAYS", "6"))
RACE_N = int(os.getenv("LOAD_RACE_N", "8"))
REQUEST_TIMEOUT_S = float(os.getenv("LOAD_HTTP_TIMEOUT_S", "30"))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{utcnow()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP plumbing (stdlib only).
# ---------------------------------------------------------------------------


class HttpClient:
    """One buyer = one opener = one cookie jar (session + JWT access)."""

    def __init__(self, base: str, ua: str):
        self.base = base.rstrip("/")
        self.ua = ua
        self.access: str | None = None
        self.csrf: str = ""
        self.username: str = ""
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._lock = threading.Lock()  # guards csrf/access during setup only

    def _cookie(self, name: str) -> str:
        for c in self._jar:
            if c.name == name:
                return c.value or ""
        return ""

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        auth: bool = False,
        csrf: bool = False,
        origin: str | None = None,
    ) -> dict:
        headers = {"User-Agent": self.ua, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.access:
            headers["Authorization"] = f"Bearer {self.access}"
        if csrf:
            headers["X-CSRFToken"] = self.csrf
            # Browser-shaped sessioned POST: Django's referer gate needs a
            # trusted Origin; same-origin (the API host itself) passes.
            headers["Origin"] = origin or self.base
            headers["Referer"] = (origin or self.base) + "/"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        t0 = time.perf_counter()
        status, payload = 0, None
        try:
            with self._opener.open(req, timeout=REQUEST_TIMEOUT_S) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            return {
                "status": 0,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"{type(exc).__name__}: {exc}",
            }
        ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return {"status": status, "ms": ms, "json": payload}

    def login(self) -> None:
        res = self.request(
            "POST", LOGIN_PATH, {"username": self.username, "password": self.password}
        )
        if res["status"] != 200 or not (res.get("json") or {}).get("access"):
            raise SetupError(
                f"login failed for {self.username}: HTTP {res['status']} "
                "(auth scope is 10/min per ident — check LOAD_LOGIN_PACE_S)"
            )
        self.access = res["json"]["access"]
        self.csrf = self._cookie("csrftoken")

    def ensure_session_csrf(self) -> None:
        """Mint/refresh the session + csrftoken pair (GET is throttle-free)."""
        self.request("GET", CART_PATH)
        self.csrf = self._cookie("csrftoken") or self.csrf


class SetupError(RuntimeError):
    """Preflight or environment problem — nothing was raced."""


_login_gate = threading.Lock()
_last_login = [0.0]


def paced_login(buyer: Buyer) -> None:
    with _login_gate:
        wait = _last_login[0] + LOGIN_PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_login[0] = time.monotonic()
    buyer.client.login()


class Buyer:
    def __init__(self, base: str, ua: str, username: str, password: str):
        self.client = HttpClient(base, ua)
        self.client.username = username
        self.client.password = password
        self.username = username


# ---------------------------------------------------------------------------
# Preflight.
# ---------------------------------------------------------------------------


def load_buyer_logins() -> list[dict]:
    raw = os.getenv("LOAD_BUYER_LOGINS", "")
    if not raw:
        raise SetupError(
            'LOAD_BUYER_LOGINS unset — expected JSON \'[{"username":...,'
            '"password":...}]\' of provisioned storefront customers'
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(f"LOAD_BUYER_LOGINS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SetupError("LOAD_BUYER_LOGINS must be a non-empty JSON array")
    for entry in parsed:
        if (
            not isinstance(entry, dict)
            or not entry.get("username")
            or not entry.get("password")
        ):
            raise SetupError("each LOAD_BUYER_LOGINS entry needs username+password")
    return parsed


def preflight(base: str, ua: str, need_buyers: int) -> dict:
    probe = HttpClient(base, ua)
    health = probe.request("GET", HEALTH_PATH)
    if health["status"] != 200:
        raise SetupError(f"{HEALTH_PATH} -> HTTP {health['status']} (box not up?)")
    checks = (health.get("json") or {}).get("checks") or {}
    if not checks.get("database"):
        raise SetupError("health checks.database is not true")
    if not checks.get("shipping_configured"):
        raise SetupError(
            "shipping_configured=false — checkout cannot price a destination; "
            "seed shipping methods before racing (see T2-plan preflight)"
        )
    slug = os.getenv("LOAD_PRODUCT_SLUG", "")
    if not slug:
        raise SetupError("LOAD_PRODUCT_SLUG unset (the race target product)")
    detail = probe.request("GET", PRODUCT_DETAIL.format(slug=slug))
    if detail["status"] != 200:
        raise SetupError(f"product {slug} -> HTTP {detail['status']} (run seed_demo?)")
    product = detail.get("json") or {}
    stock = product.get("stock")
    if not isinstance(stock, int):
        raise SetupError(f"product {slug} payload has no integer stock field")
    logins = load_buyer_logins()
    if len(logins) < need_buyers:
        raise SetupError(
            f"need >= {need_buyers} distinct buyers, LOAD_BUYER_LOGINS has {len(logins)}"
        )
    return {
        "slug": slug,
        "product_id": product.get("id"),
        "stock": stock,
        "buyers": logins,
    }


def razorpay_credentials() -> tuple[str, str]:
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key_id or not secret:
        raise SetupError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET unset — captures need the "
            "TEST-mode pair (same values the box env carries; never commit them)"
        )
    return key_id, secret


def mint_signature(razorpay_order_id: str, payment_id: str, secret: str) -> str:
    """The gateway's own scheme: HMAC-SHA256 over '<order>|<payment>'."""
    message = f"{razorpay_order_id}|{payment_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Race phases.
# ---------------------------------------------------------------------------


class IntentBudget:
    """Hard ceiling on Razorpay intent mints across the whole run."""

    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0
        self._lock = threading.Lock()

    def spend(self, n: int = 1) -> None:
        with self._lock:
            if self.used + n > self.cap:
                raise SetupError(
                    f"intent budget exhausted: {self.used}+{n} > LOAD_MAX_INTENTS={self.cap}"
                )
            self.used += n


def fire_wave(workers: list[callable]) -> list[dict]:
    """Run callables simultaneously (barrier inside each worker's task)."""
    barrier = threading.Barrier(len(workers))
    results: list[dict | None] = [None] * len(workers)

    def run(idx: int, fn: callable) -> None:
        barrier.wait(timeout=REQUEST_TIMEOUT_S + 5)
        try:
            results[idx] = fn()
        except Exception as exc:  # noqa: BLE001 - recorded per-attempt
            results[idx] = {"status": 0, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        for i, fn in enumerate(workers):
            pool.submit(run, i, fn)
    return [
        r if r is not None else {"status": 0, "error": "no result"} for r in results
    ]


def prepare_buyers(base: str, ua: str, logins: list[dict], n: int) -> list[Buyer]:
    buyers = [
        Buyer(
            base,
            ua,
            logins[i % len(logins)]["username"],
            logins[i % len(logins)]["password"],
        )
        for i in range(n)
    ]
    for b in buyers:
        paced_login(b)
        b.client.ensure_session_csrf()
    return buyers


def add_last_unit(buyer: Buyer, product_id: int) -> dict:
    res = buyer.client.request(
        "POST", CART_PATH, {"product_id": product_id, "quantity": 1}, csrf=True
    )
    return res


def checkout_call(buyer: Buyer, coupon_code: str | None) -> dict:
    payload = dict(CHECKOUT_SHIPPING)
    if coupon_code:
        payload["coupon_code"] = coupon_code
    res = buyer.client.request("POST", CHECKOUT_PATH, payload, auth=True)
    order_id = (res.get("json") or {}).get("id")
    return {
        "status": res["status"],
        "ms": res["ms"],
        "order_id": order_id,
        "error": res.get("error"),
    }


def mint_intent(buyer: Buyer, order_id: int, budget: IntentBudget) -> dict:
    budget.spend(1)
    res = buyer.client.request("POST", PAYMENT_PATH, {"order_id": order_id}, auth=True)
    body = res.get("json") or {}
    return {
        "status": res["status"],
        "ms": res["ms"],
        "razorpay_order_id": body.get("razorpay_order_id"),
        "error": res.get("error"),
    }


def capture_call(
    buyer: Buyer, rzp_order_id: str, payment_id: str, signature: str
) -> dict:
    res = buyer.client.request(
        "POST",
        VERIFY_PATH,
        {
            "razorpay_order_id": rzp_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
        auth=True,
    )
    return {"status": res["status"], "ms": res["ms"], "error": res.get("error")}


def run_parallel_checkout_wave(
    buyers: list[Buyer], coupon_code: str | None
) -> list[dict]:
    return fire_wave([lambda b=b: checkout_call(b, coupon_code) for b in buyers])


def run_parallel_capture_wave(triples: list[tuple[Buyer, str, str, str]]) -> list[dict]:
    return fire_wave(
        [lambda t=t: capture_call(t[0], t[1], t[2], t[3]) for t in triples]
    )


def phase_oversell(ctx: dict, results: dict) -> None:
    """N buyers, last unit: parallel checkout -> intents -> parallel capture."""
    need = ctx["race_n"]
    log(f"oversell: preparing {need} buyers (login paced at {LOGIN_PACE_S}s)")
    buyers = prepare_buyers(ctx["base"], ctx["ua"], ctx["buyers"], need)
    for b in buyers:
        res = add_last_unit(b, ctx["product_id"])
        if res["status"] >= 300:
            raise SetupError(
                f"cart add failed for {b.username}: HTTP {res['status']} "
                f"(stock={ctx['stock']}; a stock=1 product admits exactly 1 unit)"
            )
    log("oversell: firing parallel checkout wave")
    checkouts = run_parallel_checkout_wave(buyers, None)
    results["checkout"] = checkouts
    ok = [c for c in checkouts if c["status"] in (200, 201) and c["order_id"]]
    log(f"oversell: {len(ok)}/{need} checkouts created orders")
    if not ok:
        raise SetupError("every checkout failed — see results JSON (throttle? stock?)")

    log("oversell: minting intents (capped)")
    intents = []
    budget = ctx["budget"]
    for i, c in enumerate(checkouts):
        if c["status"] not in (200, 201) or not c["order_id"]:
            continue
        # Pair each order with the buyer at the SAME position as its
        # checkout attempt (checkouts and buyers are positional).
        buyer = buyers[i]
        intents.append((buyer, c, mint_intent(buyer, c["order_id"], budget)))
    results["intents"] = [i[2] for i in intents]
    triples = []
    for buyer, c, intent in intents:
        if intent["status"] != 200 or not intent.get("razorpay_order_id"):
            log(f"oversell: intent failed for order {c['order_id']}: {intent}")
            continue
        pay_id = f"payLOADovl{c['order_id']:08d}"
        triples.append(
            (
                buyer,
                intent["razorpay_order_id"],
                pay_id,
                mint_signature(intent["razorpay_order_id"], pay_id, ctx["secret"]),
            )
        )
    log(f"oversell: firing parallel capture wave ({len(triples)} winners)")
    captures = run_parallel_capture_wave(triples)
    results["captures"] = captures
    # Who actually captured is decided by the DB invariant checks below, not
    # by wire statuses (a replayed verify also answers 200).
    results["expected_captured"] = min(ctx["stock"], need)
    results["race_order_ids"] = [c["order_id"] for c in ok]


def phase_coupon(ctx: dict, results: dict) -> None:
    code = os.getenv("LOAD_COUPON_CODE", "")
    if not code:
        raise SetupError("LOAD_COUPON_CODE unset (the usage_limit=1 coupon)")
    need = ctx["race_n"]
    log(f"coupon: preparing {need} buyers")
    buyers = prepare_buyers(ctx["base"], ctx["ua"], ctx["buyers"], need)
    for b in buyers:
        res = add_last_unit(b, ctx["product_id"])
        if res["status"] >= 300:
            raise SetupError(f"cart add failed for {b.username}: HTTP {res['status']}")
    log(f"coupon: firing parallel checkout wave with {code}")
    checkouts = run_parallel_checkout_wave(buyers, code)
    results["checkout"] = checkouts
    ok = [c for c in checkouts if c["status"] in (200, 201) and c["order_id"]]
    log(f"coupon: {len(ok)}/{need} checkouts created orders")
    if not ok:
        raise SetupError(
            "every coupon checkout failed — is the code seeded and unlimited "
            "at CART time? (limit is enforced at capture; see T2-plan)"
        )
    budget = ctx["budget"]
    intents = []
    for i, c in enumerate(checkouts):
        if c["status"] not in (200, 201) or not c["order_id"]:
            continue
        buyer = buyers[i]
        intents.append((buyer, c, mint_intent(buyer, c["order_id"], budget)))
    results["intents"] = [t[2] for t in intents]
    triples = []
    for buyer, c, intent in intents:
        if intent["status"] != 200 or not intent.get("razorpay_order_id"):
            log(f"coupon: intent failed for order {c['order_id']}: {intent}")
            continue
        pay_id = f"payLOADcpn{c['order_id']:08d}"
        triples.append(
            (
                buyer,
                intent["razorpay_order_id"],
                pay_id,
                mint_signature(intent["razorpay_order_id"], pay_id, ctx["secret"]),
            )
        )
    log(f"coupon: firing parallel capture wave ({len(triples)} orders)")
    results["captures"] = run_parallel_capture_wave(triples)
    results["coupon_code"] = code
    results["expected_captured"] = 1  # usage_limit=1: exactly one burn
    results["race_order_ids"] = [c["order_id"] for c in ok]


def phase_double_capture(ctx: dict, results: dict) -> None:
    replays = min(ctx["replays"], 8)  # payment scope is 10/min per user
    log(f"double-capture: preparing 1 buyer, {replays} parallel replays")
    buyer = prepare_buyers(ctx["base"], ctx["ua"], ctx["buyers"], 1)[0]
    if add_last_unit(buyer, ctx["product_id"])["status"] >= 300:
        raise SetupError("cart add failed for the capture buyer")
    checkout = checkout_call(buyer, None)
    results["checkout"] = [checkout]
    if checkout["status"] not in (200, 201) or not checkout["order_id"]:
        raise SetupError(f"capture order checkout failed: HTTP {checkout['status']}")
    intent = mint_intent(buyer, checkout["order_id"], ctx["budget"])
    results["intents"] = [intent]
    if intent["status"] != 200 or not intent.get("razorpay_order_id"):
        raise SetupError(f"intent mint failed: {intent}")
    pay_id = f"payLOADdcap{checkout['order_id']:08d}"
    signature = mint_signature(intent["razorpay_order_id"], pay_id, ctx["secret"])
    log(f"double-capture: firing {replays} identical verifies of one triple")
    triples = [(buyer, intent["razorpay_order_id"], pay_id, signature)] * replays
    results["captures"] = run_parallel_capture_wave(triples)
    results["capture_order_id"] = checkout["order_id"]
    results["capture_payment_id"] = pay_id
    results["razorpay_order_id"] = intent["razorpay_order_id"]
    results["expected_captured"] = 1


# ---------------------------------------------------------------------------
# DB invariant checks — SQL, evaluation, and the two delivery modes.
# ---------------------------------------------------------------------------
#
# Every SQL string below is PostgreSQL (the UAT DB engine): ILIKE/regex and
# substr() are PG spellings, and the round-trip mode documents that. Order
# references live in StockMovement.note as "Order #<id>" (the sale writer in
# orders/inventory.py); substr(note, 8) extracts the id after "Order #".

INVARIANT_DEFS = {
    "T2-INV-1-no-negative-stock": {
        "description": "no product row may hold negative stock after the race",
        "sql": ("SELECT count(*) AS violations FROM products_products WHERE stock < 0"),
        "expect": {"rule": "scalar_eq", "column": "violations", "value": 0},
    },
    "T2-INV-2-exactly-one-captured": {
        "description": (
            "among the racing orders, captured count equals the expected "
            "winner count (last-unit oversell: min(stock, racers); coupon: 1)"
        ),
        "sql": (
            "SELECT count(*) AS captured FROM orders_order "
            "WHERE id IN ({order_ids}) AND payment_status = 'captured'"
        ),
        "expect_from": "expected_captured",
    },
    "T2-INV-3-ledger-once-per-capture": {
        "description": (
            "each captured racing order has exactly one SALE movement row per "
            "item (delta sum = -units); every loser has zero SALE rows"
        ),
        "sql": (
            "WITH racing AS ("
            "  SELECT o.id, o.payment_status,"
            "    (SELECT count(*) FROM orders_orderitem i WHERE i.order_id = o.id)"
            "      AS item_rows,"
            "    (SELECT COALESCE(sum(i.quantity), 0) FROM orders_orderitem i"
            "      WHERE i.order_id = o.id) AS item_units"
            "  FROM orders_order o WHERE o.id IN ({order_ids})),"
            " sale AS ("
            "  SELECT CAST(substr(note, 8) AS integer) AS oid, count(*) AS mv_rows,"
            "    sum(delta) AS mv_delta"
            "  FROM products_stockmovement"
            "  WHERE reason = 'sale' AND note ~ '^Order #[0-9]+$'"
            "  GROUP BY 1)"
            "SELECT count(*) AS violations FROM racing r"
            " LEFT JOIN sale m ON m.oid = r.id"
            " WHERE (r.payment_status = 'captured'"
            "        AND (COALESCE(m.mv_rows, 0) <> r.item_rows"
            "             OR COALESCE(m.mv_delta, 0) <> -r.item_units))"
            "    OR (r.payment_status <> 'captured'"
            "        AND COALESCE(m.mv_rows, 0) <> 0)"
        ),
        "expect": {"rule": "scalar_eq", "column": "violations", "value": 0},
    },
    "T2-INV-5-coupon-burned-once": {
        "description": "usage_limit=1 coupon: used_count == 1 and exactly one "
        "captured order holds it",
        "sql": (
            "SELECT"
            "  (SELECT used_count FROM orders_coupon WHERE code = {coupon_code})"
            "    AS used_count,"
            "  (SELECT count(*) FROM orders_order o"
            "     JOIN orders_coupon c ON o.coupon_id = c.id"
            "    WHERE c.code = {coupon_code}"
            "      AND o.payment_status = 'captured')"
            "    AS captured_coupon_orders"
        ),
        "expect": {
            "rule": "row_eq",
            "value": {"used_count": 1, "captured_coupon_orders": 1},
        },
    },
    "T2-INV-6-capture-moved-once": {
        "description": (
            "the double-captured order is captured with the expected payment "
            "reference and exactly ONE pending->confirmed transition"
        ),
        "sql": (
            "SELECT o.payment_status, o.razorpay_payment_id,"
            "  (SELECT count(*) FROM orders_orderstatusevent e"
            "    WHERE e.order_id = o.id AND e.from_status = 'pending'"
            "      AND e.to_status = 'confirmed') AS capture_transitions"
            " FROM orders_order o WHERE o.id = {capture_order_id}"
        ),
        "expect": {
            "rule": "row_eq",
            "value_from": {
                "payment_status": "captured",
                "razorpay_payment_id": "capture_payment_id",
                "capture_transitions": 1,
            },
        },
    },
    "T2-INV-7-no-double-decrement": {
        "description": (
            "SALE ledger rows for the double-captured order equal its item "
            "count — a replayed verify may never decrement stock twice"
        ),
        "sql": (
            "SELECT"
            "  COALESCE((SELECT count(*) FROM products_stockmovement m"
            "    WHERE m.reason = 'sale'"
            "      AND m.note = 'Order #' || {capture_order_id}), 0)"
            "    AS mv_rows,"
            "  (SELECT count(*) FROM orders_orderitem i"
            "    WHERE i.order_id = {capture_order_id}) AS item_rows"
        ),
        "expect": {
            "rule": "row_eq",
            "computed": {"mv_rows": "eq:item_rows"},
        },
    },
    "T2-INV-8-losers-holds-released": {
        "description": (
            "no racing order may still hold an ACTIVE reservation after the "
            "wave (losers released, winner converted)"
        ),
        "sql": (
            "SELECT count(*) AS active_holds FROM products_stockreservation"
            " WHERE order_id IN ({order_ids}) AND status = 'active'"
        ),
        "expect": {"rule": "scalar_eq", "column": "active_holds", "value": 0},
    },
}


def checks_for_phase(phase: str, results: dict) -> list[dict]:
    """Materialize the invariant list for one phase with this run's values."""
    checks: list[dict] = []

    def add(check_id: str, bindings: dict) -> None:
        spec = INVARIANT_DEFS[check_id]
        checks.append(
            {
                "id": check_id,
                "description": spec["description"],
                "sql": spec["sql"].format(**bindings),
                "expect": spec.get("expect"),
                "expect_from": spec.get("expect_from"),
            }
        )

    add("T2-INV-1-no-negative-stock", {})
    if phase in ("oversell", "coupon"):
        ids = results.get("race_order_ids") or []
        add("T2-INV-2-exactly-one-captured", {"order_ids": _id_list(ids)})
        add("T2-INV-3-ledger-once-per-capture", {"order_ids": _id_list(ids)})
        add("T2-INV-8-losers-holds-released", {"order_ids": _id_list(ids)})
    if phase == "coupon":
        add(
            "T2-INV-5-coupon-burned-once",
            {"coupon_code": _sql_literal(results["coupon_code"])},
        )
    if phase == "double-capture":
        add(
            "T2-INV-6-capture-moved-once",
            {
                "capture_order_id": str(int(results["capture_order_id"])),
                "capture_payment_id": _sql_literal(results["capture_payment_id"]),
            },
        )
        add(
            "T2-INV-7-no-double-decrement",
            {"capture_order_id": str(int(results["capture_order_id"]))},
        )
    # expect_from resolution (the value the operator/runner compares against).
    for c in checks:
        if c["expect_from"] == "expected_captured":
            c["expect"] = {
                "rule": "scalar_eq",
                "column": "captured",
                "value": int(results["expected_captured"]),
            }
        c.pop("expect_from", None)
        # value_from resolution: a row_eq whose expected columns name run
        # results (e.g. razorpay_payment_id -> the capture_payment_id this
        # phase minted) becomes a concrete row_eq/value before it leaves
        # this function, so both live mode and the --verify-json round trip
        # compare against real values, never against key names (auditor
        # BUG-1: the form was emitted but nothing consumed it).
        exp = c.get("expect") or {}
        if exp.get("rule") == "row_eq" and "value_from" in exp:
            c["expect"] = {
                "rule": "row_eq",
                "value": resolve_value_from(exp["value_from"], results),
            }
    return checks


def resolve_value_from(mapping: dict, context: dict) -> dict:
    """Resolve a value_from expectation against the run's results context.

    A STRING names a context key iff that key exists in the context (so
    "capture_payment_id" becomes the minted payment id); any other string
    stays a literal (so "captured" remains "captured"), and non-strings
    pass through untouched. Never raises on a missing key: an unresolved
    name compares as itself and shows up as a visible column mismatch
    rather than a crash.
    """
    resolved = {}
    for col, want in mapping.items():
        if isinstance(want, str) and want in context:
            resolved[col] = context[want]
        else:
            resolved[col] = want
    return resolved


def _id_list(ids: list) -> str:
    return ", ".join(str(int(i)) for i in ids) if ids else "-1"


def _sql_literal(value) -> str:
    """Literal for the round-trip SQL: ints as-is, strings quote-doubled."""
    if isinstance(value, bool):
        raise TypeError("bool has no literal here")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"unsupported literal type: {type(value)!r}")


def evaluate_checks(
    checks: list[dict], rows_by_id: dict, context: dict | None = None
) -> list[dict]:
    """Pure evaluation shared by live-DB mode and --verify-json mode.

    ``context`` is the run's results dict, used only when a check still
    carries the raw ``value_from`` form (live mode passes it; the emitted
    round-trip file already carries the resolved ``value`` form).
    """
    verdicts = []
    for c in checks:
        rows = rows_by_id.get(c["id"])
        expect = c.get("expect") or {}
        if rows is None:
            verdicts.append(
                {"id": c["id"], "ok": False, "reason": "no rows supplied for check"}
            )
            continue
        rule = expect.get("rule")
        ok, detail = False, "unknown rule"
        if rule == "scalar_eq":
            if not rows:
                ok, detail = False, "query returned no rows"
            else:
                got = rows[0].get(expect["column"])
                ok = got == expect["value"]
                detail = f"{expect['column']}={got!r}, expected {expect['value']!r}"
        elif rule == "row_eq" and ("value" in expect or "value_from" in expect):
            if not rows:
                ok, detail = False, "query returned no rows"
            else:
                # value_from is resolved here too (belt to checks_for_phase's
                # suspenders): a hand-written or stale checks file that still
                # names context keys must evaluate, not report "unsupported
                # expect form" and fail every run on a clean DB (BUG-1).
                want_map = (
                    expect["value"]
                    if "value" in expect
                    else resolve_value_from(expect["value_from"], context or {})
                )
                mismatches = []
                for col, want in want_map.items():
                    got = rows[0].get(col)
                    if got != want:
                        mismatches.append(f"{col}: got {got!r}, expected {want!r}")
                ok = not mismatches
                detail = "; ".join(mismatches) if mismatches else "all columns match"
        elif rule == "row_eq" and "computed" in expect:
            # Column-vs-column equality inside the same row (T2-INV-7:
            # mv_rows must equal item_rows — no hand-typed expected count).
            if not rows:
                ok, detail = False, "query returned no rows"
            else:
                mismatches = []
                for col, spec in expect["computed"].items():
                    if spec.startswith("eq:"):
                        other = spec[3:]
                        if rows[0].get(col) != rows[0].get(other):
                            mismatches.append(
                                f"{col}={rows[0].get(col)!r} != {other}="
                                f"{rows[0].get(other)!r}"
                            )
                ok = not mismatches
                detail = "; ".join(mismatches) if mismatches else "computed cols match"
        else:
            detail = f"unsupported expect form: {expect!r}"
        verdicts.append({"id": c["id"], "ok": ok, "detail": detail})
    return verdicts


def run_live_db_checks(checks: list[dict]) -> dict:
    dsn = os.getenv("LOAD_PG_DSN") or os.getenv("DATABASE_URL") or ""
    if not dsn:
        raise SetupError(
            "no DB mode: LOAD_PG_DSN/DATABASE_URL unset and no --verify-json"
        )
    try:
        import psycopg  # already a backend venv dependency (Django PG engine)
    except ImportError as exc:
        raise SetupError(
            "psycopg not importable — run with backend\\venv\\Scripts\\python "
            "or use --emit-sql/--verify-json round-trip mode"
        ) from exc
    rows_by_id: dict = {}
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        for c in checks:
            with conn.cursor() as cur:
                cur.execute(c["sql"])
                cols = [d[0] for d in cur.description]
                rows_by_id[c["id"]] = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows_by_id


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------


def write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    log(f"wrote {path}")


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------


def cmd_mint_signature(args: argparse.Namespace) -> int:
    _, secret = razorpay_credentials()
    sig = mint_signature(args.razorpay_order_id, args.payment_id, secret)
    # Prints the SIGNATURE only — never the secret.
    print(sig)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    base = os.getenv("LOAD_BASE_URL", "").rstrip("/")
    if not base:
        log("FATAL: LOAD_BASE_URL unset (no hardcoded host in this runner)")
        return 2
    ua = os.getenv("LOAD_USER_AGENT", DEFAULT_UA)
    phases = (
        ["oversell", "coupon", "double-capture"]
        if args.phase == "all"
        else [args.phase]
    )
    # Distinct buyers needed: race legs need RACE_N; double-capture needs 1.
    need_buyers = RACE_N if any(p in phases for p in ("oversell", "coupon")) else 1

    results: dict = {
        "task": "SPEC-T2a",
        "started_utc": utcnow(),
        "base_url": base,
        "phases": {},
        "intent_cap": INTENT_CAP,
        "unverified": bool(args.skip_db),
    }
    try:
        info = preflight(base, ua, need_buyers)
        # DB mode capability is checked BEFORE any HTTP wave: racing and
        # then discovering invariants cannot be evaluated would spend the
        # window (throttles, intents) for a run that cannot report.
        if not args.emit_sql and not args.skip_db:
            dsn = os.getenv("LOAD_PG_DSN") or os.getenv("DATABASE_URL") or ""
            if not dsn:
                raise SetupError(
                    "no DB mode: set LOAD_PG_DSN/DATABASE_URL for live checks, "
                    "or pass --emit-sql for the server-side round trip, or "
                    "--skip-db to race UNVERIFIED"
                )
            try:
                import psycopg  # noqa: F401
            except ImportError as exc:
                raise SetupError(
                    "psycopg not importable — run with backend\\venv\\Scripts\\python "
                    "or use --emit-sql/--verify-json round-trip mode"
                ) from exc
        need_secret = any(p in phases for p in ("oversell", "coupon", "double-capture"))
        secret = ""
        if need_secret:
            _, secret = razorpay_credentials()
        ctx = {
            "base": base,
            "ua": ua,
            "slug": info["slug"],
            "product_id": info["product_id"],
            "stock": info["stock"],
            "buyers": info["buyers"],
            "secret": secret,
            "budget": IntentBudget(INTENT_CAP),
            "race_n": RACE_N,
            "replays": CAPTURE_REPLAYS,
        }
        phase_runners = {
            "oversell": phase_oversell,
            "coupon": phase_coupon,
            "double-capture": phase_double_capture,
        }
        all_verdicts: list[dict] = []
        for phase in phases:
            log(f"=== phase: {phase} ===")
            pr: dict = {}
            phase_runners[phase](ctx, pr)
            results["phases"][phase] = pr
            checks = checks_for_phase(phase, pr)
            if args.emit_sql:
                write_json(
                    args.emit_sql.format(phase=phase)
                    if "{phase}" in args.emit_sql
                    else args.emit_sql,
                    {
                        "task": "SPEC-T2a",
                        "phase": phase,
                        "generated_utc": utcnow(),
                        "engine": "postgresql",
                        "note": (
                            "Run each sql via psql/dbshell on the DB host; "
                            "return rows as JSON [{col: value}, ...] per check "
                            "id and evaluate with --verify-json."
                        ),
                        "checks": checks,
                    },
                )
                log(f"phase {phase}: SQL emitted; invariants NOT yet evaluated")
                continue
            if args.skip_db:
                log(f"phase {phase}: --skip-db given — invariants UNVERIFIED")
                continue
            rows_by_id = run_live_db_checks(checks)
            verdicts = evaluate_checks(checks, rows_by_id, context=pr)
            all_verdicts.extend([{"phase": phase, **v} for v in verdicts])
            for v in verdicts:
                log(f"  {v['id']}: {'PASS' if v['ok'] else 'FAIL'} ({v.get('detail')})")
        results["invariant_verdicts"] = all_verdicts
        results["finished_utc"] = utcnow()
        out = args.results or f"load-runs/races-{int(time.time())}.json"
        write_json(out, results)

        if args.emit_sql:
            return 0
        if args.skip_db:
            log("NOTE: results stamped UNVERIFIED — no invariant claim is made")
            return 0
        if not all_verdicts:
            log("FATAL: HTTP phase ran but no invariant was evaluated")
            return 3
        failed = [v for v in all_verdicts if not v["ok"]]
        if failed:
            log(f"INVARIANT VIOLATIONS: {len(failed)} of {len(all_verdicts)}")
            return 1
        log(f"all {len(all_verdicts)} invariants hold")
        return 0
    except SetupError as exc:
        log(f"FATAL (setup): {exc}")
        results["finished_utc"] = utcnow()
        results["setup_error"] = str(exc)
        write_json(args.results or f"load-runs/races-{int(time.time())}.json", results)
        return 2


def cmd_verify_json(args: argparse.Namespace) -> int:
    try:
        with open(args.checks, encoding="utf-8") as fh:
            emitted = json.load(fh)
        with open(args.verify_json, encoding="utf-8") as fh:
            supplied = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"FATAL: cannot read check/rows JSON: {exc}")
        return 2
    rows_by_id = {
        entry["id"]: entry.get("rows", []) for entry in supplied.get("checks", [])
    }
    verdicts = evaluate_checks(
        emitted.get("checks", []), rows_by_id, context=supplied.get("context") or {}
    )
    failed = [v for v in verdicts if not v["ok"]]
    for v in verdicts:
        log(f"  {v['id']}: {'PASS' if v['ok'] else 'FAIL'} ({v.get('detail')})")
    if args.out:
        write_json(args.out, {"verdicts": verdicts})
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute race phases")
    run_p.add_argument(
        "--phase",
        choices=["all", "oversell", "coupon", "double-capture"],
        default="all",
    )
    run_p.add_argument(
        "--skip-db",
        action="store_true",
        help="EXPLICITLY skip DB invariants (results stamped UNVERIFIED)",
    )
    run_p.add_argument(
        "--emit-sql",
        metavar="PATH",
        help="write this phase's invariant SQL+expectations to PATH "
        "({phase} placeholder allowed) instead of evaluating",
    )
    run_p.add_argument("--results", metavar="PATH", help="results JSON path")
    run_p.set_defaults(func=cmd_run)

    mint_p = sub.add_parser(
        "mint-signature", help="print the gateway HMAC for a triple (secret stays env)"
    )
    mint_p.add_argument("--razorpay-order-id", required=True)
    mint_p.add_argument("--payment-id", required=True)
    mint_p.set_defaults(func=cmd_mint_signature)

    ver_p = sub.add_parser(
        "verify-json", help="evaluate operator-supplied query rows against expectations"
    )
    ver_p.add_argument("--checks", required=True, help="emitted checks JSON")
    ver_p.add_argument("--verify-json", required=True, help="operator rows JSON")
    ver_p.add_argument("--out", help="optional verdicts JSON path")
    ver_p.set_defaults(func=cmd_verify_json)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SetupError as exc:
        # Missing env/credentials: a clean FATAL line, never a traceback.
        log(f"FATAL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
