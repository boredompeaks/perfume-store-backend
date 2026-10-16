#!/usr/bin/env python
"""SPEC-T1a red-team runner: families 1-5 (BUILD-QUEUE Phase T, T1 items 1-5).

Attacks the REAL UAT API (owner-approved, test mode only, no real money ever)
over HTTPS from this sandbox with stdlib urllib only - no third-party deps.
One runner for the whole first half; each attack records attempted /
invariant / result / severity so the report table is evidence-backed, never
transcribed from memory.

Safety rails baked in:
- browser User-Agent (the edge 403s the default Python UA);
- every request is a small JSON/HTML probe - no bulk writes, no brute force,
  no payload above a few hundred bytes (the UAT box may OOM; that is accepted
  and informative, but volume stays capped);
- coupon-scope traffic waits for a fresh 60s window before the 12-shot burst,
  so the throttle proof measures the scope and not our own earlier spend;
- nothing targets a non-test environment: the base URL is explicit per run.

Evidence JSON (default scripts/t1a_evidence.json, untracked) is rewritten after
every family phase, so a partial run still leaves usable rows behind.
Exit code: 0 when the run completes (rows carry PASS/FAIL/BLOCKED), 1 when the
run itself broke (no connectivity / no rows written).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

DEFAULT_BASE = "https://api.boredompeaks.me"
# The deployed storefront origin the SPA posts from; must be listed in the
# box's CSRF_TRUSTED_ORIGINS for checkout to work at all (probed live: a
# trusted origin passes the referer/origin gate, an evil one is refused by
# name). Sessioned POSTs are sent browser-shaped: Origin + Referer + the
# csrftoken replay, otherwise Django's referer check refuses them before the
# application layer and the row would measure the wrong thing.
TRUSTED_ORIGIN = "https://test.boredompeaks.me"
# Edge bot rule 403s the stdlib UA (measured); a browser-shaped UA is what the
# storefront itself sends, so attacks run with it to reach the application.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 25
SNIP = 170
UNSET = object()

# Every response body the run observed, kept for the POST-F1 payload scan
# (session material must never appear in any body, so nothing is filtered).
CAPTURED = []


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    # 30x must surface as data (Location + status), never be followed into an
    # admin login page that would mask the original status code.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    """One cookie jar = one browser identity (session = cart credential)."""

    def __init__(self, base, name="anon"):
        self.base = base.rstrip("/")
        self.name = name
        self.jar = CookieJar()
        self.opener = urlrequest.build_opener(
            _NoRedirect(), urlrequest.HTTPCookieProcessor(self.jar)
        )

    def request(
        self,
        path,
        method="GET",
        body=UNSET,
        headers=None,
        content_type="application/json",
    ):
        data = None
        if body is not UNSET:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Connection": "close",
        }
        if data is not None and content_type:
            hdrs["Content-Type"] = content_type
        if headers:
            hdrs.update(headers)
        req = urlrequest.Request(
            self.base + path, data=data, headers=hdrs, method=method
        )
        try:
            resp = self.opener.open(req, timeout=TIMEOUT)
            status, raw, rhdrs = resp.status, resp.read(), resp.headers
        except urlerror.HTTPError as e:
            status, raw, rhdrs = e.code, e.read(), e.headers
        except Exception as e:  # noqa: BLE001 - recorded as a row, never hidden
            return {"status": None, "body": f"TRANSPORT-ERROR {e!r}", "headers": {}}
        out = {
            "status": status,
            "body": raw.decode("utf-8", errors="replace"),
            "headers": {k.lower(): v for k, v in rhdrs.items()},
        }
        CAPTURED.append((self.name, out["body"]))
        return out

    def cookie_values(self, names=("sessionid", "csrftoken")):
        out = {}
        for c in self.jar:
            if c.name in names:
                out.setdefault(c.name, set()).add(c.value)
        return out


def snip(s, n=SNIP):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def same(a, b):
    return a["status"] == b["status"] and a["body"] == b["body"]


class Ledger:
    def __init__(self, base):
        self.base = base
        self.rows = []
        self.env = []
        self.coupon_times = []
        # Shared with Http.request's capture: every body seen this run.
        self.all_bodies = CAPTURED

    def add(self, row_id, family, attempted, invariant, request, response, result,
            severity="n/a"):
        self.rows.append(
            {
                "id": row_id,
                "family": family,
                "attempted": attempted,
                "invariant": invariant,
                "request": request,
                "response": response,
                "result": result,
                "severity": severity,
            }
        )

    def env_add(self, probe, request, response):
        self.env.append(
            {"probe": probe, "request": request, "response": response}
        )

    def note_coupon(self, resp, when=None):
        self.coupon_times.append(when or time.time())

    def wait_fresh_coupon_window(self, pad=62):
        """Sleep until the oldest coupon call in the current window expires."""
        if not self.coupon_times:
            return 0.0
        oldest = min(self.coupon_times)
        wait = oldest + pad - time.time()
        if wait > 0:
            time.sleep(wait)
        self.coupon_times.clear()
        return wait

    def counts(self):
        by_family = {}
        for r in self.rows:
            f = by_family.setdefault(
                r["family"], {"pass": 0, "fail": 0, "blocked": 0}
            )
            key = {"PASS": "pass", "FAIL": "fail", "BLOCKED": "blocked"}[r["result"]]
            f[key] += 1
        return {
            "pass": sum(1 for r in self.rows if r["result"] == "PASS"),
            "fail": sum(1 for r in self.rows if r["result"] == "FAIL"),
            "blocked": sum(1 for r in self.rows if r["result"] == "BLOCKED"),
            "total": len(self.rows),
            "by_family": by_family,
        }


def verdict(ok):
    return "PASS" if ok else "FAIL"


def envelope_ok(resp, statuses=(401, 403)):
    """DRF error envelope at an expected refusal status, no data payload."""
    if resp["status"] not in statuses:
        return False
    try:
        body = json.loads(resp["body"])
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body and "code" in body


def browser_csrf(jar, origin=None, with_token=True):
    """Headers a same-origin browser sends on a sessioned POST.

    origin=None sends NO Origin/Referer (the server-to-server shape - Django
    then refuses on the referer check); an explicit foreign origin models a
    cross-site attacker that somehow replays the cookie.
    """
    hdrs = {}
    if origin:
        hdrs["Origin"] = origin
        hdrs["Referer"] = origin + "/"
    if with_token:
        token = next((c.value for c in jar if c.name == "csrftoken"), "")
        if token:
            hdrs["X-CSRFToken"] = token
    return hdrs


def ensure_session(H):
    """Give an identity its cart session (ids the CSRF token comes from)."""
    if not any(c.name == "sessionid" for c in H.jar):
        H.request("/api/v1/store/cart/")


# --------------------------------------------------------------------------
# STEP ZERO: environment capability inventory
# --------------------------------------------------------------------------
def phase_env(H: Http, F: Ledger, base):
    probes = [
        ("public-health", "GET", "/health/", UNSET, {}),
        ("store-config", "GET", "/api/v1/store/config/", UNSET, {}),
        ("products-list-empty", "GET", "/api/v1/store/products/", UNSET, {}),
        (
            "register-new-account",
            "POST",
            "/api/v1/account/register/",
            {
                "username": f"rt_t1a_{uuid.uuid4().hex[:8]}",
                "email": "rt_t1a@redteam.invalid",
                "password": "Xk9#vmQ2pLw7z",
            },
            {},
        ),
        (
            "login-unverified-account",
            "POST",
            "/api/v1/account/storefront/login/",
            {"username": "rt_t1a_c1", "password": "Xk9#vmQ2pLw7z"},
            {},
        ),
        (
            "login-seeded-demo-customer",
            "POST",
            "/api/v1/account/storefront/login/",
            {"username": "demo-customer", "password": "demo-account"},
            {},
        ),
        ("seeded-demo-slug", "GET", "/api/v1/store/products/demo-plenty-perfume/",
         UNSET, {}),
        (
            "username-probe-existing",
            "GET",
            "/api/v1/account/username-available/?username=rt_t1a_c1",
            UNSET,
            {},
        ),
        (
            "resend-verification-smtp-state",
            "POST",
            "/api/v1/account/resend-verification/",
            {"email": "rt_t1a@redteam.invalid"},
            {},
        ),
        ("frontend-health", "GET", "https://test.boredompeaks.me/health/", UNSET, {}),
    ]
    for probe, method, path, body, headers in probes:
        if path.startswith("https://"):
            ext = Http("https://test.boredompeaks.me", "edge")
            resp = ext.request("/health/", method="GET")
        else:
            resp = H.request(path, method=method, body=body, headers=headers)
        F.env_add(
            probe,
            f"{method} {path}",
            f"{resp['status']} {snip(resp['body'], 240)}",
        )

    # Edge bot rule: default Python UA measured against the same endpoint.
    req = urlrequest.Request(
        base + "/health/",
        headers={"User-Agent": "Python-urllib/3.x", "Accept": "*/*"},
    )
    try:
        r = urlrequest.urlopen(req, timeout=TIMEOUT)
        note = f"{r.status} allowed (no UA rule observed)"
    except urlerror.HTTPError as e:
        note = f"{e.code} {snip(e.read().decode(errors='replace'), 120)}"
    except Exception as e:  # noqa: BLE001
        note = f"transport {e!r}"
    F.env_add("default-python-ua-probe", "GET /health/ (Python-urllib UA)", note)

    # Session cookie attributes on the cart-creating GET (transport hardening).
    resp = H.request("/api/v1/store/cart/")
    sc = resp["headers"].get("set-cookie", "")
    F.env_add(
        "cart-session-cookie-flags",
        "GET /api/v1/store/cart/",
        f"{resp['status']} Set-Cookie: {snip(sc, 240)}",
    )


# --------------------------------------------------------------------------
# Family 1: order lifecycle / capture
# --------------------------------------------------------------------------
def phase1(H: Http, F: Ledger, bare: Http):
    webhook_path = "/api/v1/webhooks/razorpay/"
    good_event = {"event": "payment.captured", "payload": {"x": "y"}}
    variants = [
        ("no-signature-header", good_event, {}),
        (
            "bad-signature-64hex",
            good_event,
            {"X-Razorpay-Signature": "a" * 64},
        ),
        (
            "alg-confusion-none",
            good_event,
            {"X-Razorpay-Signature": "none"},
        ),
        (
            "truncated-signature-63",
            good_event,
            {"X-Razorpay-Signature": "b" * 63},
        ),
        (
            "empty-signature",
            good_event,
            {"X-Razorpay-Signature": ""},
        ),
        (
            "garbage-body-badsig",
            {"nonsense": True},
            {"X-Razorpay-Signature": "deadbeef" * 8},
        ),
    ]
    webhook_resps = []
    for name, event, headers in variants:
        # bare identity: a gateway call carries no cookies at all.
        resp = bare.request(webhook_path, method="POST", body=event,
                            headers=headers)
        webhook_resps.append(resp)
    non2xx = all(
        r["status"] is not None and r["status"] >= 400 for r in webhook_resps
    )
    uniform = all(same(r, webhook_resps[0]) for r in webhook_resps[1:])
    F.add(
        "F1-W1",
        "1",
        "Six forged webhook deliveries in one batch: unsigned, 64-hex wrong "
        "sig, alg-confusion 'none', 63-char truncated, empty sig, garbage "
        "body + bad sig.",
        "HMAC-SHA256 over the raw body is checked with hmac.compare_digest "
        "BEFORE any parsing application (orders/webhooks.py); with the secret "
        "unset the endpoint answers one uniform 503 and never accepts a "
        "delivery (fail-closed, .env.example RAZORPAY_WEBHOOK_SECRET).",
        "POST /api/v1/webhooks/razorpay/ x6 (forged signatures)",
        "; ".join(
            f"{n}={r['status']}"
            for n, r in zip([v[0] for v in variants], webhook_resps)
        )
        + f"; byte-identical across variants: {uniform}",
        verdict(non2xx),
    )

    # Concurrent double-delivery (the replay-race shape at the transport edge).
    def _fire(_):
        return bare.request(
            webhook_path,
            method="POST",
            body=good_event,
            headers={"X-Razorpay-Signature": "c" * 64},
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        pair = list(ex.map(_fire, range(2)))
    both_refused = all(r["status"] is not None and r["status"] >= 400 for r in pair)
    F.add(
        "F1-W2",
        "1",
        "Two identical forged deliveries fired concurrently (double-capture "
        "race at the HTTP layer).",
        "Neither delivery may be accepted: signature check precedes the "
        "idempotency no-op key on the order's payment dimension "
        "(orders/webhooks.py), so nothing is recorded twice.",
        "POST /api/v1/webhooks/razorpay/ x2 parallel",
        f"statuses {[r['status'] for r in pair]}",
        verdict(both_refused),
    )

    forged_verify = {
        "razorpay_order_id": "order_nonexistent_rt",
        "razorpay_payment_id": "pay_nonexistent_rt",
        "razorpay_signature": "d" * 64,
    }
    tampered = dict(forged_verify, amount=1, currency="INR")
    v1 = bare.request(
        "/api/v1/store/orders/payment/verify/", "POST", forged_verify
    )
    v2 = bare.request("/api/v1/store/orders/payment/verify/", "POST", tampered)
    v3 = bare.request(
        "/api/orders/payment/verify/", "POST", forged_verify  # legacy alias
    )
    ensure_session(H)
    v4 = H.request(
        "/api/v1/store/orders/payment/verify/",
        "POST",
        forged_verify,
        headers=browser_csrf(H.jar, origin=TRUSTED_ORIGIN),
    )
    ok = (
        envelope_ok(v1)
        and same(v1, v2)
        and same(v1, v3)
        and same(v1, v4)
        and v1["status"] == 401
    )
    F.add(
        "F1-V1",
        "1",
        "Forged browser-verify quadruple (bogus order/payment ids + 64-hex "
        "signature): plain, amount=1/currency tamper fields, legacy mount, "
        "and a same-origin sessioned identity (Origin+csrftoken replayed).",
        "verify_payment is IsAuthenticated + 'payment' scope "
        "(orders/views/payment.py): an unauthenticated caller is refused by "
        "the auth stack before any signature work - identically on both "
        "mounts and regardless of session or tampered amount/currency fields "
        "(the server derives both from the Order row).",
        "POST /api/v1/store/orders/payment/verify/ x4 "
        "(forged, tampered, legacy, sessioned)",
        f"{v1['status']}/{v2['status']}/{v3['status']}/{v4['status']}; "
        f"all byte-identical {same(v1, v2) and same(v1, v3) and same(v1, v4)}; "
        f"body {snip(v1['body'])}",
        verdict(ok),
    )

    pay_body = {
        "order_id": "order_nonexistent_rt",
        "amount": 1,
        "currency": "INR",
    }
    p1 = bare.request("/api/v1/store/orders/payment/", "POST", pay_body)
    p2 = bare.request("/api/orders/payment/", "POST", pay_body)
    ok = (
        p1["status"] == 401
        and p2["status"] == 401
        and same(p1, p2)
        and envelope_ok(p1)
    )
    F.add(
        "F1-P1",
        "1",
        "Payment-intent mint attempt for a non-existent order with tampered "
        "amount/currency, via the v1 mount and the legacy alias.",
        "create_payment is IsAuthenticated + 'payment' scope; amount/currency "
        "come from the caller's own Order row, never the body "
        "(orders/views/payment.py), and both mounts share one view object "
        "(config/urls.py).",
        "POST /api/v1/store/orders/payment/ + /api/orders/payment/",
        f"{p1['status']} vs {p2['status']}; byte-identical "
        f"{same(p1, p2)}; body {snip(p1['body'])}",
        verdict(ok),
    )

    refund_body = {"amount": 1, "reason": "redteam"}
    r1 = bare.request("/api/v1/admin/orders/1/refund/", "POST", refund_body)
    r2 = bare.request("/api/admin/orders/1/refund/", "POST", refund_body)
    ok = r1["status"] in (401, 403) and same(r1, r2) and envelope_ok(
        r1, statuses=(401, 403)
    )
    F.add(
        "F1-R1",
        "1",
        "Refund without capture: anonymous POST to the refund seam (v1 + "
        "legacy alias), amount 1.00 on order id 1.",
        "The refund seam is gated by its permission class before any "
        "state-machine check (config/urls.py mounts; orders/refund.py), so a "
        "caller who never captured cannot reach the money path at all.",
        "POST /api/v1/admin/orders/1/refund/ + /api/admin/orders/1/refund/",
        f"{r1['status']} vs {r2['status']}; byte-identical {same(r1, r2)}; "
        f"body {snip(r1['body'])}",
        verdict(ok),
    )

    blocked = [
        ("F1-B1", "Replay an already-captured webhook (idempotency no-op key "
         "on the order's payment dimension).",
         "Second identical delivery no-ops; stock ledger moves once.",
         "RAZORPAY_WEBHOOK_SECRET is unset on the live env (every delivery "
         "503s), so no delivery can carry a valid signature; also needs a "
         "captured order from seed_demo."),
        ("F1-B2", "Replay a browser-verify for an already-paid order.",
         "verify_payment's payment-dimension guard no-ops the replay.",
         "Requires an authenticated account + a paid order: seed_demo never "
         "ran on this env and self-registered accounts stay inactive until "
         "an emailed token we cannot receive."),
        ("F1-B3", "Webhook + verify fired concurrently (double-capture race, "
         "ledger moves once).",
         "select_for_update() on the order row + payment dimension makes one "
         "writer of the sale (orders/webhooks.py, orders/inventory.py).",
         "Needs a valid webhook secret AND a real capturable order - both "
         "missing on this deployment."),
        ("F1-B4", "Valid-signature capture for a non-existent / cancelled "
         "order.",
         "Unknown order id is refused; cancelled order stays cancelled "
         "(payment state machine in orders/state.py).",
         "Valid signatures require the server-side RAZORPAY_WEBHOOK_SECRET; "
         "cancelled orders require seeded/created orders."),
        ("F1-B5", "Amount/currency tampering inside a valid verify/webhook "
         "payload.",
         "Server recomputes amount from the Order row in the store currency; "
         "gateway-computed HMAC covers the real payload.",
         "Needs a real order + gateway credentials (RAZORPAY_KEY_ID/SECRET "
         "appear unset: no payment intent can even be minted to observe)."),
        ("F1-B6", "Double refund + refund-without-capture at the staff seam "
         "logic level.",
         "Refund state machine (orders/refunds.py) refuses both; refund is "
         "row-locked in transaction.atomic().",
         "Needs a staff session (no staff account is reachable: no seeded "
         "users, no inbox for self-registration verification)."),
        ("F1-B7", "Browser-verify replay after expiry / across users.",
         "Signature is bound to the gateway order id; ownership is the "
         "caller's own order.",
         "Needs an authenticated owner of a paid order (deployment state "
         "missing as above)."),
    ]
    for rid, attempted, invariant, why in blocked:
        F.add(
            rid,
            "1",
            attempted,
            invariant,
            "not executable on this deployment",
            f"BLOCKED: {why}",
            "BLOCKED",
        )


# --------------------------------------------------------------------------
# Family 2: inventory
# --------------------------------------------------------------------------
def phase2(F: Ledger, bare: Http):
    adjust = {"sku_or_slug": "demo-plenty-perfume", "delta": -99}
    a1 = bare.request(
        "/api/v1/store/products/inventory/adjustments/", "POST", adjust
    )
    a2 = bare.request("/api/products/inventory/adjustments/", "POST", adjust)
    ok = a1["status"] in (401, 403) and same(a1, a2) and envelope_ok(
        a1, statuses=(401, 403)
    )
    F.add(
        "F2-A1",
        "2",
        "Negative stock via the adjust API: anonymous POST delta=-99 on the "
        "v1 mount and the legacy alias.",
        "HasInventoryAdjust gates the endpoint through permission_classes "
        "(products/views.py) - an anonymous caller never reaches the delta "
        "validation, let alone the stock row.",
        "POST .../products/inventory/adjustments/ x2 (v1 + legacy)",
        f"{a1['status']} vs {a2['status']}; byte-identical {same(a1, a2)}; "
        f"body {snip(a1['body'])}",
        verdict(ok),
    )

    create = {
        "name": "rt-negative-stock",
        "price": "1.00",
        "stock": -5,
        "slug": "rt-negative-stock",
    }
    c1 = bare.request("/api/v1/store/products/", "POST", create)
    c2 = bare.request("/api/products/", "POST", create)
    ok = c1["status"] in (401, 403) and same(c1, c2) and envelope_ok(
        c1, statuses=(401, 403)
    )
    F.add(
        "F2-A2",
        "2",
        "Product create with stock=-5 (negative stock via the write API), "
        "anon on both mounts.",
        "products.write capability is checked before serializer validation "
        "(HasProductsWriteOrReadOnly), so the negative value never reaches a "
        "model field.",
        "POST /api/v1/store/products/ + /api/products/ (stock=-5)",
        f"{c1['status']} vs {c2['status']}; byte-identical {same(c1, c2)}; "
        f"body {snip(c1['body'])}",
        verdict(ok),
    )

    u1 = bare.request(
        "/api/v1/store/products/zz-rt-nonexistent/", "PUT",
        {"price": "0.01", "stock": -1},
    )
    d1 = bare.request(
        "/api/v1/store/products/zz-rt-nonexistent/", "DELETE"
    )
    ok = u1["status"] in (401, 403, 405) and d1["status"] in (401, 403, 405)
    F.add(
        "F2-A3",
        "2",
        "Update/delete attempt on a product detail route as anon (stock "
        "mutation through the write verbs).",
        "Write verbs carry the products capability check; a nonexistent slug "
        "must not be distinguishable from a delisted one on a read, and a "
        "write must be refused before the slug lookup (MERCHANT-DELIST).",
        "PUT/DELETE /api/v1/store/products/zz-rt-nonexistent/",
        f"PUT={u1['status']} DELETE={d1['status']}; PUT body "
        f"{snip(u1['body'])}",
        verdict(ok),
    )

    adm = bare.request("/admin/")
    loc = adm["headers"].get("location", "")
    ok = adm["status"] in (302, 401, 403) and (
        adm["status"] != 302 or "admin/login" in loc
    )
    F.add(
        "F2-A4",
        "2",
        "Admin inline stock adjust: reach the Django admin changelist as "
        "anon (the surface that carries the stock inline).",
        "The admin mount is session+MFA gated (accounts/admin.py MFAAdminSite "
        "+ Q-S4 login throttle); anon gets only the login redirect, never a "
        "form or a row.",
        "GET /admin/",
        f"{adm['status']} location={snip(adm['headers'].get('location', '-'))}",
        verdict(ok),
    )

    blocked = [
        ("F2-B1", "N parallel checkouts of the last unit (oversell race).",
         "commit_order_sale's summed re-check under select_for_update makes "
         "exactly one winner; stock never goes negative.",
         "No purchasable product exists (seed_demo never ran; product list "
         "count=0) and no authenticated checkout identity is reachable."),
        ("F2-B2", "Admin adjust-stock racing a concurrent capture.",
         "StockMovement ledger stays consistent: every capture has exactly "
         "one row-set and stock equals opening +/- ledger.",
         "Needs staff session (inventory role) + a capturable order."),
        ("F2-B3", "Reservation expiry during an in-flight payment.",
         "expire_reservations releases only stale holds; verify_payment "
         "re-checks live stock at capture (SPEC-12-02).",
         "Needs a created order with reservations (seed/state missing)."),
        ("F2-B4", "Negative stock via the admin inline / admin form.",
         "Model/validation backstop refuses negative stock on every surface.",
         "No staff credentials exist on this deployment (see env verdict)."),
        ("F2-B5", "Summed re-check twin-line shape (two order lines that "
         "each pass the per-line backstop but exceed the sum).",
         "commit_order_sale re-checks the summed demand at payment time.",
         "Needs a cart holding two lines of real products - nothing to add "
         "(product list empty)."),
        ("F2-B6", "Adjust-stock as catalogue/inventory/customer roles "
         "(capability matrix on the mutation surface).",
         "inventory.adjust is inventory+admin only (common/roles.py "
         "CAPABILITY_ROLES).",
         "No role-bearing accounts can be created: roles are assigned by "
         "staff and seed_demo never ran."),
    ]
    for rid, attempted, invariant, why in blocked:
        F.add(
            rid, "2", attempted, invariant, "not executable on this deployment",
            f"BLOCKED: {why}", "BLOCKED",
        )


# --------------------------------------------------------------------------
# Family 3: coupons
# --------------------------------------------------------------------------
def phase3(H: Http, F: Ledger, S: Http):
    """H = session identity with a cart; S = bare anonymous (cartless)."""
    ensure_session(H)
    # Browser-shaped sessioned POST: same-origin Origin/Referer + the
    # csrftoken replay, so the request reaches the coupon layer instead of
    # Django's referer check (measured separately in family 4).
    hdrs = browser_csrf(H.jar, origin=TRUSTED_ORIGIN)
    # Cartless preview oracle: two absent codes, no session at all.
    p1 = S.request("/api/v1/store/orders/apply-coupon/", "POST", {"code": "NOSUCH99"})
    F.note_coupon(p1)
    p2 = S.request("/api/v1/store/orders/apply-coupon/", "POST", {"code": "OTHER99"})
    F.note_coupon(p2)
    ok = same(p1, p2) and p1["status"] >= 400
    F.add(
        "F3-C1",
        "3",
        "Cartless preview oracle: apply-coupon with two different absent "
        "codes and NO session/cart.",
        "Without a cart the endpoint answers one uniform not-found before it "
        "ever reads the code (TIER1-GUEST-DEADEND checkout module), so the "
        "error channel cannot distinguish coupon codes to a cartless caller.",
        "POST /api/v1/store/orders/apply-coupon/ x2 (no cookies)",
        f"{p1['status']} vs {p2['status']}; byte-identical {same(p1, p2)}; "
        f"body {snip(p1['body'])}",
        verdict(ok),
    )

    # Session + empty cart: absent-code uniformity and malformed-input shapes.
    absent_a = H.request(
        "/api/v1/store/orders/apply-coupon/", "POST", {"code": "NOSUCH99"},
        headers=hdrs,
    )
    F.note_coupon(absent_a)
    absent_b = H.request(
        "/api/v1/store/orders/apply-coupon/", "POST", {"code": "NOSUCH00"},
        headers=hdrs,
    )
    F.note_coupon(absent_b)
    ok = same(absent_a, absent_b) and absent_a["status"] < 500
    F.add(
        "F3-C2",
        "3",
        "Coupon code enumeration via error differences: two absent codes "
        "against a real (session) cart.",
        "Absent codes take one identical error path (uniform coupon errors), "
        "so a caller cannot enumerate which codes exist by diffing bodies.",
        "POST /api/v1/store/orders/apply-coupon/ (cart, NOSUCH99 vs NOSUCH00)",
        f"{absent_a['status']} vs {absent_b['status']}; byte-identical "
        f"{same(absent_a, absent_b)}; body {snip(absent_a['body'])}",
        verdict(ok),
    )

    malformed = [
        ("empty-code", {"code": ""}),
        ("oversize-code", {"code": "X" * 300}),
        ("lowercase-twin", {"code": "nosuch99"}),
        ("quote-injection", {"code": "x' OR '1'='1"}),
        ("markup-payload", {"code": "<script>alert(1)</script>"}),
    ]
    mal_resps = []
    for name, payload in malformed:
        r = H.request("/api/v1/store/orders/apply-coupon/", "POST", payload,
                      headers=hdrs)
        F.note_coupon(r)
        mal_resps.append((name, r))
    no_500 = all(r["status"] is not None and r["status"] < 500
                 for _, r in mal_resps)
    twin_ok = same(mal_resps[3][1], absent_a) and same(
        mal_resps[4][1], absent_a
    )
    F.add(
        "F3-C3",
        "3",
        "Malformed coupon payloads: empty code, 300-char code, lowercase "
        "twin of an absent code, SQL-quote payload, markup payload.",
        "Every malformed input stays inside the 4xx envelope - no 5xx, no "
        "database error text, and code-shaped payloads answer identically to "
        "the plain absent code (no reflection/oracle).",
        "POST /api/v1/store/orders/apply-coupon/ x5",
        "; ".join(f"{n}={r['status']}" for n, r in mal_resps)
        + f"; injection payloads byte-identical to absent: {twin_ok}",
        verdict(no_500 and twin_ok),
    )

    # Deliberate throttle proof LAST, in a guaranteed-fresh window.
    F.wait_fresh_coupon_window()
    burst = [
        H.request(
            "/api/v1/store/orders/apply-coupon/",
            "POST",
            {"code": f"BURST{i:02d}"},
            headers=hdrs,
        )
        for i in range(12)
    ]
    for r in burst:
        F.note_coupon(r)
    throttled = [r for r in burst if r["status"] == 429]
    retry_after = None
    if throttled:
        retry_after = throttled[0]["headers"].get("retry-after")
    F.add(
        "F3-C4",
        "3",
        "Coupon scope throttle: 12 rapid apply-coupon calls in a fresh "
        "60-second window (rate 10/min by default, THROTTLE_COUPON_RATE).",
        "ScopedRateThrottle on the 'coupon' scope refuses beyond the budget "
        "with 429 (settings REST_FRAMEWORK DEFAULT_THROTTLE_CLASSES), so the "
        "preview surface cannot be sprayed.",
        "POST /api/v1/store/orders/apply-coupon/ x12 burst (same-origin "
        "sessioned)",
        f"statuses={[r['status'] for r in burst]}; 429 count="
        f"{len(throttled)}/12; first 429 body "
        f"{snip(throttled[0]['body']) if throttled else '-'}; "
        f"Retry-After={retry_after}",
        verdict(bool(throttled)),
    )

    # Instrument control: a DIFFERENT dedicated tight scope (username-probe,
    # 5/min default) from this same client. If this never refuses either, the
    # question moves from the coupon view to how budgets are keyed/holder on
    # this deployment (worker-count x per-process cache is the documented
    # open finding; the knobs are unreadable from outside).
    u_statuses = []
    for i in range(30):
        r = H.request(
            "/api/v1/account/username-available/?username=rt_t1a_ctrl%02d" % i
        )
        u_statuses.append(r["status"])
        if r["status"] == 429:
            break
    first_u = next(
        (i for i, s in enumerate(u_statuses, 1) if s == 429), None
    )
    F.add(
        "F3-C5",
        "3",
        "Throttle instrument control: up to 30 rapid username-available "
        "probes (scope username-probe, default 5/min) from the same client.",
        "A dedicated ScopedRateThrottle scope must refuse beyond its budget "
        "from one client - the control that tells a coupon-view anomaly from "
        "a deployment-wide throttle-cache/keys question.",
        "GET /api/v1/account/username-available/ x<=30 burst",
        f"statuses={u_statuses}; first refusal at shot {first_u}",
        verdict(first_u is not None),
    )

    # Sustained coupon spray in the SAME window as C4 when C4 saw no
    # refusal: finds the effective budget instead of guessing it.
    if not throttled:
        extra = []
        for i in range(25):
            r = H.request(
                "/api/v1/store/orders/apply-coupon/",
                "POST",
                {"code": f"SPRAY{i:02d}"},
                headers=hdrs,
            )
            F.note_coupon(r)
            extra.append(r["status"])
            if r["status"] == 429:
                break
        first_x = next(
            (12 + i for i, s in enumerate(extra) if s == 429), None
        )
        F.add(
            "F3-C6",
            "3",
            "Sustained coupon spray continuing C4's fresh window: up to 25 "
            "more apply-coupon shots from the same client (up to 37 total).",
            "A sprayed preview surface must be refused within a bounded "
            "number of shots; the refusal index IS the effective per-client "
            "budget (the shipped default is 10/min).",
            "POST /api/v1/store/orders/apply-coupon/ x<=25 (window "
            "continuation)",
            f"statuses={extra}; first refusal at shot {first_x} "
            f"window-total={len(burst) + len(extra)}",
            verdict(first_x is not None),
        )

    blocked = [
        ("F3-B1", "Double-burn a usage_limit=1 coupon via webhook+verify "
         "concurrency.",
         "Coupon spend is committed once under select_for_update; the second "
         "capturer sees limit reached (orders/inventory.py + state machine).",
         "Needs DEMO-ONCE (seed never ran) + a valid webhook secret."),
        ("F3-B2", "Over-limit timing: limit reached between cart-add and "
         "capture - must be recorded-not-prevented + visible.",
         "The over-limit condition is recorded at capture, not silently "
         "prevented, and shows in the order/response.",
         "Needs a usage-limited coupon with real orders in flight."),
        ("F3-B3", "Cartless preview oracle against an EXISTING code "
         "(present vs absent differential).",
         "Cartless callers get one uniform answer regardless of code "
         "existence.",
         "No coupon rows exist on this deployment (seeded DEMO-ONCE absent), "
         "so only the absent-vs-absent half was executable."),
        ("F3-B4", "Reuse a coupon after order cancel.",
         "Cancel path returns the coupon spend; reuse goes through normal "
         "validation once.",
         "Needs an order with a coupon applied and a cancel seam session."),
        ("F3-B5", "Coupon enumeration across existing vs absent codes with a "
         "cart.",
         "Existing and absent codes produce no diffable existence oracle.",
         "No coupon exists to enumerate against (deployment state missing)."),
    ]
    for rid, attempted, invariant, why in blocked:
        F.add(
            rid, "3", attempted, invariant, "not executable on this deployment",
            f"BLOCKED: {why}", "BLOCKED",
        )


# --------------------------------------------------------------------------
# Family 4: guest / cart
# --------------------------------------------------------------------------
def phase4(H: Http, F: Ledger, cart_ref: Http):
    checkout = "/api/v1/store/orders/checkout/"
    ensure_session(H)
    trusted = browser_csrf(H.jar, origin=TRUSTED_ORIGIN)
    full_payload = {
        "full_name": "Red Team",
        "phone": "9999999999",
        "address": "1 Attack Street",
        "city": "Probe City",
        "state": "Probe State",
        "pincode": "123456",
        "guest_email": "attacker@redteam.invalid",
    }
    variants = [
        ("full-payload", full_payload, "application/json"),
        ("empty-object", {}, "application/json"),
        ("missing-guest-email", {k: v for k, v in full_payload.items()
                                 if k != "guest_email"}, "application/json"),
        ("guest-email-invalid", dict(full_payload,
                                     guest_email="not-an-email"),
         "application/json"),
        ("guest-email-oversize", dict(full_payload, guest_email="a" * 300 + "@x.tld"),
         "application/json"),
        ("guest-email-list", dict(full_payload, guest_email=["a@b.c"]),
         "application/json"),
        ("guest-email-null", dict(full_payload, guest_email=None),
         "application/json"),
        ("malformed-json-body", b'{"bad"', "application/json"),
        ("text-plain-content-type", json.dumps(full_payload).encode(),
         "text/plain"),
    ]
    resps = []
    for name, payload, ctype in variants:
        r = cart_ref.request(
            checkout, "POST", payload, content_type=ctype
        )
        resps.append((name, r))
    first = resps[0][1]
    uniform = all(same(r, first) for _, r in resps[1:])
    G = [f"{n}={r['status']}" for n, r in resps]
    F.add(
        "F4-G1",
        "4",
        "Guest-checkout refusal bypass battery (no session): full payload, "
        "empty body object, missing/invalid/oversize/list/null guest_email, "
        "malformed JSON, text/plain content type.",
        "TIER1-GUEST-DEADEND: create_order answers ONE uniform refusal "
        "before the atomic block opens - no payload shape may differentiate "
        "the answer (orders/views/checkout.py _guest_checkout_refused).",
        "POST /api/v1/store/orders/checkout/ x9 (payload variants)",
        "; ".join(G) + f"; all byte-identical: {uniform}",
        verdict(uniform and first["status"] == 403),
    )

    # Session-riding variants: the four CSRF axes a browser-session POST can
    # take - same-origin+token (reaches the view), no Origin at all, a foreign
    # Origin, and trusted Origin without the token.
    s1 = H.request(checkout, "POST", full_payload, headers=trusted)
    ok_s1 = same(s1, first)
    F.add(
        "F4-G2",
        "4",
        "Session-trick checkout: the same refusal POST riding the cart "
        "session cookie, browser-shaped (trusted Origin + csrftoken replay).",
        "Session presence changes nothing: the deadend refusal is "
        "user-independent and byte-identical with or without a session once "
        "the request is same-origin (orders/views/checkout.py).",
        "POST checkout (session + Origin trusted + X-CSRFToken)",
        f"{s1['status']}; byte-identical to no-session battery: {ok_s1}; "
        f"body {snip(s1['body'])}",
        verdict(ok_s1),
    )

    s2 = H.request(checkout, "POST", full_payload)  # no Origin, no token
    ok_s2 = (
        s2["status"] in (401, 403)
        and "Referer checking failed" in s2["body"]
    )
    F.add(
        "F4-G3",
        "4",
        "Session-riding checkout with NO Origin/Referer and no CSRF token "
        "(server-to-server shaped request wearing a stolen cookie).",
        "The CSRF referer check refuses an HTTPS sessioned POST that presents "
        "no Origin/Referer before the view runs (SPEC-17-03 stack), and the "
        "refusal carries no order/cart data.",
        "POST checkout (session cookie, no Origin, no X-CSRFToken)",
        f"{s2['status']} {snip(s2['body'])}",
        verdict(ok_s2),
    )

    s7 = H.request(
        checkout,
        "POST",
        full_payload,
        headers=browser_csrf(H.jar, origin="https://evil.example"),
    )
    ok_s7 = (
        s7["status"] == 403 and "Origin checking failed" in s7["body"]
    )
    F.add(
        "F4-G7",
        "4",
        "Cross-site session riding: stolen cookie replayed from "
        "https://evil.example WITH a valid csrftoken attached.",
        "Origin checking refuses any origin outside CSRF_TRUSTED_ORIGINS by "
        "name - possession of both cookie and token cannot move the request "
        "off the storefront origin (SPEC-17-03; Django referer/origin gate).",
        "POST checkout (session + Origin https://evil.example + token)",
        f"{s7['status']} {snip(s7['body'])}",
        verdict(ok_s7),
    )

    s8 = H.request(
        checkout,
        "POST",
        full_payload,
        headers={"Origin": TRUSTED_ORIGIN, "Referer": TRUSTED_ORIGIN + "/"},
    )
    ok_s8 = s8["status"] == 403 and "CSRF token missing" in s8["body"]
    F.add(
        "F4-G8",
        "4",
        "Trusted origin but NO csrftoken (double-submit binding).",
        "The same-origin check passing is not enough: the token must match "
        "the cookie (SessionCartCSRFAuthentication / SPEC-17-03), otherwise "
        "the request is refused before the view.",
        "POST checkout (session + trusted Origin, no X-CSRFToken)",
        f"{s8['status']} {snip(s8['body'])}",
        verdict(ok_s8),
    )

    s3 = H.request(
        checkout,
        "POST",
        full_payload,
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    ok_s3 = s3["status"] in (401, 403)
    F.add(
        "F4-G4",
        "4",
        "Auth+anon mix: garbage Bearer token alongside the session cookie.",
        "An invalid bearer is rejected by the JWT authentication stack "
        "uniformly (401 envelope) - the mix never downgrades to a different "
        "code path or leaks order state.",
        "POST checkout (session + Authorization: Bearer garbage)",
        f"{s3['status']} {snip(s3['body'])}",
        verdict(ok_s3),
    )

    key = f"rt-key-{uuid.uuid4().hex}"
    k1 = cart_ref.request(
        checkout, "POST", full_payload, headers={"Idempotency-Key": key}
    )
    k2 = cart_ref.request(
        checkout, "POST", full_payload, headers={"Idempotency-Key": key}
    )
    k3 = H.request(
        checkout, "POST", full_payload,
        headers={**trusted, "Idempotency-Key": key},
    )
    ok = same(k1, first) and same(k2, first) and same(k3, first)
    F.add(
        "F4-G5",
        "4",
        "Keyed replay: same Idempotency-Key on repeated checkout POSTs "
        "(bare, repeat, session+CSRF).",
        "The refusal precedes any key consumption - nothing is created and "
        "no 'key already spent' oracle exists while guest checkout is a "
        "dead-end (orders/views/checkout.py).",
        "POST checkout x3 with one Idempotency-Key",
        f"{k1['status']}/{k2['status']}/{k3['status']}; all byte-identical "
        f"to the plain refusal: {ok}",
        verdict(ok),
    )

    # Concurrency: 10 simultaneous anonymous checkouts, all refused alike.
    def _co(_):
        return cart_ref.request(checkout, "POST", full_payload)

    with ThreadPoolExecutor(max_workers=10) as ex:
        par = list(ex.map(_co, range(10)))
    par_ok = all(same(r, first) for r in par)
    F.add(
        "F4-G6",
        "4",
        "Ten concurrent anonymous checkout POSTs (parallel refusal race).",
        "The deadend refuses everything before opening a transaction - "
        "concurrency cannot buy a single created row or a divergent answer.",
        "POST checkout x10 parallel",
        f"statuses {[r['status'] for r in par]}; all byte-identical: "
        f"{par_ok}",
        verdict(par_ok),
    )

    # ---- cart enumeration / cross-session --------------------------------
    jarA, jarB, jarC = (
        Http(cart_ref.base, "A"),
        Http(cart_ref.base, "B"),
        Http(cart_ref.base, "C"),
    )
    ids = {}
    for tag, jar in (("A", jarA), ("B", jarB), ("C", jarC)):
        r1 = jar.request("/api/v1/store/cart/")
        r2 = jar.request("/api/v1/store/cart/")
        try:
            ids[tag] = (json.loads(r1["body"]).get("id"),
                        json.loads(r2["body"]).get("id"))
        except ValueError:
            ids[tag] = (None, None)
    distinct = (
        len({v[0] for v in ids.values()}) == 3
        and all(v[0] == v[1] for v in ids.values())
    )
    F.add(
        "F4-X1",
        "4",
        "Cart id enumeration via session replay: three fresh sessions each "
        "GET the cart twice (stability + distinctness).",
        "The cart is keyed by its own session - each session always resolves "
        "to ITS cart id, never to another session's, and ids never rotate "
        "under a stable cookie.",
        "GET /api/v1/store/cart/ x6 (3 sessions)",
        f"ids={ids}; per-session stable + cross-session distinct: "
        f"{distinct}",
        verdict(distinct),
    )

    # jarB's own token: the double-submit must match the cookie that rides
    # the request, so H's token would be refused as a mismatch.
    trusted_b = browser_csrf(jarB.jar, origin=TRUSTED_ORIGIN)
    item_resps = []
    for item_id in (1, 2, 3):
        item_resps.append(
            (f"DEL/{item_id}",
             jarB.request(f"/api/v1/store/cart/{item_id}/", "DELETE",
                          headers=trusted_b))
        )
    item_resps.append(
        ("PATCH/1",
         jarB.request(
             "/api/v1/store/cart/1/",
             "PATCH",
             {"quantity": 7},
             headers=trusted_b,
         ))
    )
    no_data = all(
        r["status"] in (401, 403, 404) and "total" not in r["body"]
        for _, r in item_resps
    )
    uniform404 = all(
        r["status"] == item_resps[0][1]["status"] for _, r in item_resps
    )
    F.add(
        "F4-X2",
        "4",
        "Cross-session cart item access: session B deletes/patches item ids "
        "1-3 (ids it does not own).",
        "cart_item_detail resolves items only inside the caller's own cart "
        "(cart/views.py), so foreign or unknown item ids share one miss "
        "answer and no other cart's totals leak.",
        "DELETE /api/v1/store/cart/{1,2,3}/ + PATCH /cart/1/ (session B)",
        "; ".join(f"{n}={r['status']}" for n, r in item_resps)
        + f"; uniform status: {uniform404}; no data: {no_data}",
        verdict(no_data and uniform404),
    )

    add_resp = jarB.request(
        "/api/v1/store/cart/",
        "POST",
        {"product_id": "not-a-number", "quantity": 1},
        headers=trusted_b,
    )
    add_resp2 = jarB.request(
        "/api/v1/store/cart/",
        "POST",
        {"product_id": 99999999, "quantity": 1},
        headers=trusted_b,
    )
    ok = all(
        r["status"] in (400, 404) for r in (add_resp, add_resp2)
    ) and not any("Traceback" in r["body"] for r in (add_resp, add_resp2))
    F.add(
        "F4-X3",
        "4",
        "Cart add of unknown products (session B, with CSRF): non-numeric "
        "product_id and a huge numeric id.",
        "Unknown products are refused inside 4xx - the view's "
        "ValueError->404 guard means a garbage id must never 500 and the "
        "refusal must not disclose whether any id exists (cart/views.py).",
        "POST /api/v1/store/cart/ x2 (product_id not-a-number / 99999999)",
        f"garbage-id={add_resp['status']} {snip(add_resp['body'])}; "
        f"huge-id={add_resp2['status']} {snip(add_resp2['body'])}",
        verdict(ok),
    )

    # Session fixation half: forged/garbage sessionid must not 500.
    fix = Http(cart_ref.base, "fixated")
    # Session fixation probes. urllib only sends cookies it was SET with, so
    # both legs replay a Cookie header manually: a stolen (but genuine)
    # sessionid and a fabricated one.
    fix_first = fix.request("/api/v1/store/cart/")
    try:
        fix_cart = json.loads(fix_first["body"]).get("id")
    except ValueError:
        fix_cart = None
    planted = next(
        (c.value for c in fix.jar if c.name == "sessionid"), ""
    )

    def replay(cookie_value):
        req = urlrequest.Request(
            cart_ref.base + "/api/v1/store/cart/",
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Cookie": f"sessionid={cookie_value}",
            },
        )
        try:
            r = urlrequest.urlopen(req, timeout=TIMEOUT)
            status, body = r.status, r.read().decode()
        except urlerror.HTTPError as e:
            status, body = e.code, e.read().decode()
        except Exception as e:  # noqa: BLE001
            status, body = None, repr(e)
        try:
            cart_id = json.loads(body).get("id")
        except ValueError:
            cart_id = None
        return status, cart_id

    st_legit, cart_legit = replay(planted)
    forged_value = uuid.uuid4().hex
    st_forged, cart_forged = replay(forged_value)
    live_carts = {v[0] for v in ids.values()} | {cart_legit, fix_cart}
    ok = (
        st_legit == 200
        and cart_legit == fix_cart
        and st_forged == 200
        and cart_forged not in live_carts
    )
    F.add(
        "F4-S1",
        "4",
        "Session fixation probes: (a) a captured sessionid replayed from a "
        "fresh client, (b) a fabricated sessionid replayed the same way.",
        "The session cookie IS the cart credential by design (SPEC-17-03), "
        "carried only via Set-Cookie (never a response body - see F4-P1): a "
        "genuine cookie reproduces exactly the session it was minted for (no "
        "cross-session confusion) and a forged value gains no existing cart - "
        "the server mints a fresh one instead of erroring.",
        f"GET /api/v1/store/cart/ with replayed sessionid "
        f"{planted[:8]}... and forged {forged_value[:8]}...",
        f"legit: {st_legit} -> cart {cart_legit} (own cart {fix_cart}); "
        f"forged: {st_forged} -> cart {cart_forged} (not in live set "
        f"{sorted(x for x in live_carts if x)})",
        verdict(ok),
    )
    F.add(
        "F4-S2",
        "4",
        "Login-time session rotation after a planted cart session "
        "(fixation completion).",
        "Django cycles the session key on login, so a planted sessionid "
        "cannot become an authenticated session the attacker still holds.",
        "would POST /api/v1/account/storefront/login/ with the planted "
        "cookie attached",
        "BLOCKED: no credential can complete a login on this deployment "
        "(seed users absent; self-registered accounts inactive until an "
        "emailed token we cannot receive)",
        "BLOCKED",
    )

    # ---- POST-F1 regression: session material must never appear in payloads
    secret_values = set()
    for jar in (cart_ref, jarA, jarB, jarC, fix, H):
        for name, vals in jar.cookie_values().items():
            secret_values.update(vals)
    leaked = []
    for where, body in F.all_bodies:
        for val in secret_values:
            if val and val in body:
                leaked.append(where)
    ok = not leaked
    F.add(
        "F4-P1",
        "4",
        "POST-F1 regression: scan every response body captured in this run "
        "(webhooks, verify, checkout, cart, coupon, register, config) for any "
        "sessionid/csrftoken value observed in Set-Cookie.",
        "The session key never enters any payload - cookies are the only "
        "carrier (SPEC-17-03 / POST-F1 finding); a body echo would re-open "
        "the leak.",
        f"body scan over {len(F.all_bodies)} captured payloads, "
        f"{len(secret_values)} cookie values",
        f"leaks={leaked if leaked else 'none'}",
        verdict(ok),
    )

    F.add(
        "F4-B1",
        "4",
        "Cart clear-on-capture cannot be tricked into clearing another "
        "user's cart.",
        "Capture clears only the ordering user's cart inside the capture "
        "transaction (orders flow), never a cart keyed by another session.",
        "not executable on this deployment",
        "BLOCKED: needs a successful capture (no capturable order, no "
        "authenticated buyer, no webhook secret)",
        "BLOCKED",
    )


# --------------------------------------------------------------------------
# Family 5: RBAC matrix + delist uniformity
# --------------------------------------------------------------------------
def phase5(H: Http, F: Ledger, bare: Http):
    matrix = [
        # (id, method, path, data)
        ("dashboard", "GET", "/api/v1/admin/dashboard/", UNSET),
        ("dashboard-legacy", "GET", "/admin/dashboard/", UNSET),
        ("audit-log", "GET", "/api/v1/admin/audit-log/", UNSET),
        ("audit-log-legacy", "GET", "/admin/audit-log/", UNSET),
        ("admin-health", "GET", "/api/v1/admin/health/", UNSET),
        ("orders-list", "GET", "/api/v1/admin/orders/", UNSET),
        ("orders-list-legacy", "GET", "/api/admin/orders/", UNSET),
        ("order-detail", "GET", "/api/v1/admin/orders/1/", UNSET),
        ("order-detail-legacy", "GET", "/api/admin/orders/1/", UNSET),
        ("order-fulfil", "POST", "/api/v1/admin/orders/1/fulfill/", {}),
        ("order-cancel", "POST", "/api/v1/admin/orders/1/cancel/", {}),
        ("order-refund", "POST", "/api/v1/admin/orders/1/refund/",
         {"amount": 1}),
        ("global-search", "GET", "/admin/search/?q=redteam", UNSET),
        ("saved-filter-save", "POST",
         "/admin/saved-filters/products/product/save/", {"name": "rt"}),
        ("saved-filter-delete", "POST", "/admin/saved-filters/delete/1/", {}),
        ("inventory-adjust", "POST",
         "/api/v1/store/products/inventory/adjustments/", {"delta": -1}),
        ("product-create", "POST", "/api/v1/store/products/",
         {"name": "rt", "price": "1.00"}),
        ("product-delist-put", "PUT",
         "/api/v1/store/products/zz-rt-nonexistent/", {"is_active": False}),
        ("store-orders-list", "GET", "/api/v1/store/orders/", UNSET),
        ("store-order-detail", "GET", "/api/v1/store/orders/1/", UNSET),
    ]
    results = []
    for rid, method, path, data in matrix:
        r = bare.request(path, method=method, body=data) if data is not UNSET \
            else bare.request(path, method=method)
        results.append((rid, r))
    bad = [
        (rid, r["status"])
        for rid, r in results
        if r["status"] not in (401, 403, 302)
    ]
    no_data = True
    for rid, r in results:
        if r["status"] in (200, 201, 202, 204):
            no_data = False
    F.add(
        "F5-M1",
        "5",
        "RBAC matrix, anonymous identity x 20 capability endpoints "
        "(dashboard, audit, admin health, orders read/fulfil/cancel/refund "
        "on both mounts, global search, saved filters, inventory adjust, "
        "product create/delist, store orders).",
        "Every staff/capability surface answers 401/403 through "
        "permission_classes (or the admin's login redirect) and never 2xx "
        "with data to an anonymous caller; legacy and v1 mounts are one view "
        "object so they refuse identically (config/urls.py).",
        "20x anonymous GET/POST against admin + write surfaces",
        "; ".join(f"{rid}={r['status']}" for rid, r in results)
        + f"; non-2xx everywhere: {no_data}; anomalies: {bad or 'none'}",
        verdict(no_data and not bad),
    )

    # Session identity variant: a session cookie must buy no capabilities.
    ensure_session(H)
    sess_hdrs = browser_csrf(H.jar, origin=TRUSTED_ORIGIN)
    sess_matrix = [
        ("orders-list", "GET", "/api/v1/admin/orders/", UNSET),
        ("order-refund", "POST", "/api/v1/admin/orders/1/refund/",
         {"amount": 1}),
        ("inventory-adjust", "POST",
         "/api/v1/store/products/inventory/adjustments/", {"delta": -1}),
        ("product-create", "POST", "/api/v1/store/products/",
         {"name": "rt", "price": "1.00"}),
    ]
    sess_res = []
    for rid, method, path, data in sess_matrix:
        hdrs = sess_hdrs if method == "POST" else {}
        r = (
            H.request(path, method=method, body=data, headers=hdrs)
            if data is not UNSET
            else H.request(path, method=method)
        )
        sess_res.append((rid, r))
    ok = all(r["status"] in (401, 403) for _, r in sess_res)
    F.add(
        "F5-M2",
        "5",
        "RBAC matrix, anonymous+session identity x 4 sensitive endpoints "
        "(orders read, refund, inventory adjust, product create).",
        "A cart session confers no staff capability: authorization is the "
        "capability check on the user, and the anonymous user stays "
        "anonymous however the cookie is presented.",
        "4x session-cookie GET/POST on admin + write surfaces",
        "; ".join(f"{rid}={r['status']}" for rid, r in sess_res)
        + f"; first body {snip(sess_res[0][1]['body'])}",
        verdict(ok),
    )

    # IDOR probes on the customer order read + guest token read.
    o1 = H.request("/api/v1/store/orders/1/")
    o2 = H.request("/api/v1/store/orders/999999/")
    ok = o1["status"] == 401 and same(o1, o2)
    F.add(
        "F5-I1",
        "5",
        "IDOR on customer order detail: GET another user's order id (1) and "
        "an absurd id, anonymously.",
        "order_detail is IsAuthenticated and uniform-401 before any lookup, "
        "so the answer cannot even confirm whether the id exists (no "
        "existence oracle on ids).",
        "GET /api/v1/store/orders/1/ + /999999/",
        f"{o1['status']} vs {o2['status']}; byte-identical {same(o1, o2)}; "
        f"body {snip(o1['body'])}",
        verdict(ok),
    )

    g1 = H.request(
        "/api/v1/store/orders/guest/",
        headers={"X-Guest-Order-Token": "e" * 64},
    )
    g2 = H.request(
        "/api/v1/store/orders/guest/RT-000000/",
        headers={"X-Guest-Order-Token": "e" * 64},
    )
    g3 = H.request(
        "/api/v1/store/orders/guest/",
        headers={"X-Guest-Order-Token": "f" * 128},
    )
    ok = (
        g1["status"] == g2["status"] >= 400
        and same(g1, g2)
        and g3["status"] in (400, 404)
    )
    F.add(
        "F5-I2",
        "5",
        "Guest-token IDOR: junk 64-char token against the token route and "
        "the token+order-number route; plus a 128-char oversize token.",
        "Every guest lookup miss answers the one _guest_lookup_miss body "
        "(orders/views/checkout.py); the token length bound refuses oversize "
        "input before it reaches the index.",
        "GET /api/v1/store/orders/guest[/<number>/] with X-Guest-Order-Token",
        f"token={g1['status']} by-number={g2['status']} "
        f"byte-identical={same(g1, g2)}; oversize={g3['status']} "
        f"{snip(g3['body'])}",
        verdict(ok),
    )

    # ---- delist uniformity: the nonexistent side, live --------------------
    d1 = H.request("/api/v1/store/products/zz-rt-absent-one/")
    d2 = H.request("/api/v1/store/products/zz-rt-absent-two/")
    d1l = H.request("/api/products/zz-rt-absent-one/")
    ok = d1["status"] == d2["status"] == 404 and same(d1, d2) and same(d1, d1l)
    F.add(
        "F5-D1",
        "5",
        "Delist uniformity, nonexistent side: two absent slugs on the v1 "
        "mount plus the legacy alias twin of one.",
        "MERCHANT-DELIST: product_detail serves the same 404 for a "
        "nonexistent and a delisted row (products/views.py), and both mounts "
        "share the view - byte-identical bodies across slugs and mounts.",
        "GET /api/v1/store/products/{zz-rt-absent-one,two}/ + legacy twin",
        f"{d1['status']}/{d2['status']}/{d1l['status']}; v1 twins identical "
        f"{same(d1, d2)}; legacy identical {same(d1, d1l)}; body "
        f"{snip(d1['body'])}",
        verdict(ok),
    )

    base_list = H.request("/api/v1/store/products/")
    filter_variants = [
        {"search": "zz-rt-nothing", "category": "zz", "min_price": "1",
         "max_price": "2", "ordering": "-price"},
        {"ordering": "name"},
        {"ordering": ";drop table"},
        {"min_price": "notanumber"},
        {"search": "<script>alert(1)</script>"},
    ]
    list_resps = []
    for params in filter_variants:
        q = "?" + urlencode(params)
        list_resps.append((q, H.request("/api/v1/store/products/" + q)))
    ident = [
        same(r, base_list) for q, r in list_resps if "notanumber" not in q
    ]
    bad_price = next(
        r for q, r in list_resps if "notanumber" in q
    )
    ok = (
        base_list["status"] == 200
        and all(ident)
        and bad_price["status"] == 400
    )
    F.add(
        "F5-D2",
        "5",
        "REST search/filters/relations half of delist uniformity: five "
        "filter/ordering/search variants (incl. adversarial ordering and "
        "markup) against the empty catalogue, vs the clean list.",
        "The list base queryset is filter(is_active=True), so no downstream "
        "search/ordering/filter can reintroduce a hidden row (products/"
        "views.py MERCHANT-DELIST); invalid numeric filters answer one "
        "designed validation error, markup/SQL-shaped strings are inert "
        "values.",
        "GET /api/v1/store/products/ x6 (clean + 5 variants)",
        f"clean={base_list['status']}; byte-identical to clean per variant "
        f"{ident}; variant statuses="
        f"{[r['status'] for _, r in list_resps]}; invalid-price="
        f"{bad_price['status']} {snip(bad_price['body'])}",
        verdict(ok),
    )

    blocked = [
        ("F5-B1", "RBAC matrix for the 7 staff roles (support, catalogue, "
         "inventory, marketing, finance, admin, superadmin) + a customer "
         "role across every capability endpoint.",
         "Each role answers 200 only on its CAPABILITY_ROLES cells and 403 "
         "elsewhere (common/roles.py).",
         "No role-bearing account can be created from outside: seed_demo "
         "never ran (demo-* logins all 401) and self-registration yields an "
         "inactive account gated on an emailed token this sandbox cannot "
         "receive."),
        ("F5-B2", "IDOR on order detail ACROSS real users (user A's token "
         "reading user B's order; guest token reading another guest's "
         "order).",
         "Object-level check: only the owner (or a valid guest token for "
         "that row) can read an order (orders/views/order.py).",
         "No orders exist on this deployment and no authenticated identity "
         "is obtainable."),
        ("F5-B3", "Delisted-vs-nonexistent byte comparison on the PUBLIC "
         "detail read (the live half of MERCHANT-DELIST).",
         "The 404 body for a delisted row is byte-identical to a nonexistent "
         "one.",
         "No product row exists to delist: the catalogue is empty and only "
         "staff can write products."),
        ("F5-B4", "Delisted row exclusion from REST search/filter/relation "
         "responses (list half).",
         "filter(is_active=True) base queryset keeps delisted rows out of "
         "every list/relation response whatever the params.",
         "Requires at least one delisted row; catalogue is empty."),
    ]
    for rid, attempted, invariant, why in blocked:
        F.add(
            rid, "5", attempted, invariant, "not executable on this deployment",
            f"BLOCKED: {why}", "BLOCKED",
        )


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", default="scripts/t1a_evidence.json")
    ap.add_argument(
        "--only",
        default="env,f1,f2,f3,f4,f5",
        help="comma list of phases: env,f1,f2,f3,f4,f5",
    )
    args = ap.parse_args()

    phases = [p.strip() for p in args.only.split(",") if p.strip()]
    F = Ledger(args.base)
    H = Http(args.base, "identity")   # main identity: anonymous + session
    cart_ref = Http(args.base, "cartless-ref")  # fresh jar per battery shot
    # Rows this invocation owns; the rest of a previous run's evidence is
    # preserved so a recovery run (--only f4) never clobbers finished phases.
    fam_by_phase = {"f1": "1", "f2": "2", "f3": "3", "f4": "4", "f5": "5"}
    refreshed_fams = {fam_by_phase[p] for p in phases if p in fam_by_phase}

    def dump():
        rows, env_rows = F.rows, F.env
        try:
            with open(args.out, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = None
        if old and old.get("meta", {}).get("base") == args.base:
            if "env" not in phases:
                env_rows = old.get("env", [])
            rows = (
                [r for r in old.get("rows", []) if r["family"] not in refreshed_fams]
                + F.rows
            )
        doc = {
            "meta": {
                "base": args.base,
                "ua": UA,
                "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                "task": "SPEC-T1a families 1-5",
            },
            "counts": {
                "pass": sum(1 for r in rows if r["result"] == "PASS"),
                "fail": sum(1 for r in rows if r["result"] == "FAIL"),
                "blocked": sum(1 for r in rows if r["result"] == "BLOCKED"),
                "total": len(rows),
            },
            "env": env_rows,
            "rows": rows,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=True)

    runner = {
        "env": lambda: phase_env(H, F, args.base),
        "f1": lambda: phase1(H, F, cart_ref),
        "f2": lambda: phase2(F, cart_ref),
        "f3": lambda: phase3(H, F, cart_ref),
        "f4": lambda: phase4(H, F, cart_ref),
        "f5": lambda: phase5(H, F, cart_ref),
    }
    try:
        for name in phases:
            runner[name.strip()]()
            dump()
            print(
                f"[{name}] rows={F.counts()['total']} "
                f"pass={F.counts()['pass']} fail={F.counts()['fail']} "
                f"blocked={F.counts()['blocked']}"
            )
    except Exception as e:  # noqa: BLE001 - keep whatever rows exist
        dump()
        print(f"RUN BROKE at {phases}: {e!r}; partial evidence written")
        return 1
    c = F.counts()
    print(
        f"DONE total={c['total']} pass={c['pass']} fail={c['fail']} "
        f"blocked={c['blocked']} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
