#!/usr/bin/env python3
"""Targeted red-team probes for unresolved API gaps (A-F).

Run:
    python red_team_probe.py

Logs:
    console + red_team_probe_<timestamp>.log
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import threading
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


# -----------------------------
# Config
# -----------------------------
BASE_URL = os.getenv("REDTEAM_BASE_URL", "http://127.0.0.1:8000")
RACE_PRODUCT_ID = int(os.getenv("RACE_PRODUCT_ID", "3"))
CHECKOUT_BURST_N = int(os.getenv("CHECKOUT_BURST_N", "12"))
VERIFY_RACE_N = int(os.getenv("VERIFY_RACE_N", "4"))
COUPON_BURST_N = int(os.getenv("COUPON_BURST_N", "13"))
RECOVERY_BURST_N = int(os.getenv("RECOVERY_BURST_N", "8"))
TIMEOUT_SECONDS = int(os.getenv("REDTEAM_TIMEOUT_SECONDS", "30"))

CUSTOMER1_USERNAME = os.getenv("REDTEAM_CUSTOMER1_USERNAME", "customer")
CUSTOMER1_PASSWORD = os.getenv("REDTEAM_CUSTOMER1_PASSWORD", "Malkani123")
CUSTOMER1_EMAIL = os.getenv("REDTEAM_CUSTOMER1_EMAIL", "customer@example.com")

CUSTOMER2_USERNAME = os.getenv("REDTEAM_CUSTOMER2_USERNAME", "customer2")
CUSTOMER2_PASSWORD = os.getenv("REDTEAM_CUSTOMER2_PASSWORD", "Malkani123")
CUSTOMER2_EMAIL = os.getenv("REDTEAM_CUSTOMER2_EMAIL", "customer2@example.com")

VERIFY_USER_CANDIDATES = [
    os.getenv("REDTEAM_VERIFY_USER_1", CUSTOMER1_USERNAME),
    os.getenv("REDTEAM_VERIFY_USER_2", CUSTOMER2_USERNAME),
    os.getenv("REDTEAM_VERIFY_USER_3", "customer3"),
    os.getenv("REDTEAM_VERIFY_USER_4", "customer4"),
    os.getenv("REDTEAM_VERIFY_USER_5", "customer5"),
]
VERIFY_PASSWORD = os.getenv("REDTEAM_VERIFY_PASSWORD", "Malkani123")

RECOVERY_EMAIL = os.getenv("REDTEAM_RECOVERY_EMAIL", CUSTOMER1_EMAIL)


# -----------------------------
# Logging
# -----------------------------
LOG_LOCK = threading.Lock()
LOG_FILE = Path(__file__).resolve().parent / f"red_team_probe_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"


def log(msg: str = "") -> None:
    line = msg if isinstance(msg, str) else str(msg)
    with LOG_LOCK:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def section(title: str) -> None:
    log("\n" + "=" * 88)
    log(title)
    log("=" * 88)


@dataclass
class Resp:
    method: str
    path: str
    status: int | None
    body: str
    json_data: dict[str, Any] | list[Any] | None
    headers: dict[str, str]
    error: str | None


class ApiClient:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/") + "/"
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.access_token: str | None = None

    def clone(self) -> "ApiClient":
        cloned = ApiClient(self.base_url)
        cloned.access_token = self.access_token
        for c in self.cookies:
            cloned.cookies.set_cookie(_clone_cookie(c))
        return cloned

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Resp:
        url = urljoin(self.base_url, path.lstrip("/"))
        body_bytes = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.access_token:
            headers["Authorization"] = " ".join(["Bearer", self.access_token])
        csrf_token = _cookie_value(self.cookies, "csrftoken")
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and csrf_token:
            headers["X-CSRFToken"] = csrf_token
        if extra_headers:
            headers.update(extra_headers)

        req = Request(url=url, data=body_bytes, headers=headers, method=method.upper())

        try:
            with self.opener.open(req, timeout=TIMEOUT_SECONDS) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return _build_resp(method, path, r.getcode(), raw, dict(r.headers), None)
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return _build_resp(method, path, e.code, raw, dict(e.headers or {}), None)
        except URLError as e:
            return _build_resp(method, path, None, "", {}, f"URLError: {e}")
        except Exception as e:  # pragma: no cover (probe resiliency)
            return _build_resp(method, path, None, "", {}, f"{type(e).__name__}: {e}")


# -----------------------------
# Helpers
# -----------------------------
def _build_resp(method: str, path: str, status: int | None, body: str, headers: dict[str, Any], error: str | None) -> Resp:
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
    return Resp(
        method=method,
        path=path,
        status=status,
        body=body,
        json_data=parsed,
        headers={str(k): str(v) for k, v in (headers or {}).items()},
        error=error,
    )


def _clone_cookie(c: Cookie) -> Cookie:
    return Cookie(
        version=c.version,
        name=c.name,
        value=c.value,
        port=c.port,
        port_specified=c.port_specified,
        domain=c.domain,
        domain_specified=c.domain_specified,
        domain_initial_dot=c.domain_initial_dot,
        path=c.path,
        path_specified=c.path_specified,
        secure=c.secure,
        expires=c.expires,
        discard=c.discard,
        comment=c.comment,
        comment_url=c.comment_url,
        rest=dict(c._rest),
        rfc2109=c.rfc2109,
    )


def _cookie_value(jar: CookieJar, name: str) -> str | None:
    for c in jar:
        if c.name == name:
            return c.value
    return None


def summarize_statuses(tag: str, responses: list[Resp]) -> None:
    counts = Counter(r.status for r in responses)
    log(f"[{tag}] status counts: {dict(sorted(counts.items(), key=lambda kv: str(kv[0])))}")


def log_response(prefix: str, resp: Resp) -> None:
    log(f"{prefix} -> status={resp.status}, error={resp.error}")
    log(f"{prefix} BODY (full):")
    log(resp.body if resp.body else "<empty>")


def checkout_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "full_name": "Red Team Buyer",
        "phone": "9876543210",
        "address": "221 Probe Street",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400001",
    }
    payload.update(overrides)
    return payload


def ensure_login(client: ApiClient, username: str, password: str, email: str | None = None) -> bool:
    login_resp = client.request("POST", "/api/accounts/login/", {"username": username, "password": password})
    token = None
    if isinstance(login_resp.json_data, dict):
        token = login_resp.json_data.get("access")
    if login_resp.status == 200 and token:
        client.access_token = token
        log(f"login ok: {username}")
        return True

    log(f"login failed for {username} (status={login_resp.status}); trying self-register fallback")
    if email is None:
        email = f"{username}@example.com"
    reg_resp = client.request(
        "POST",
        "/api/accounts/register/",
        {"username": username, "email": email, "password": password},
    )
    log_response(f"register {username}", reg_resp)

    login_resp2 = client.request("POST", "/api/accounts/login/", {"username": username, "password": password})
    token2 = None
    if isinstance(login_resp2.json_data, dict):
        token2 = login_resp2.json_data.get("access")
    if login_resp2.status == 200 and token2:
        client.access_token = token2
        log(f"login ok after register: {username}")
        return True

    log_response(f"login retry {username}", login_resp2)
    return False


def ensure_cart_has_item(client: ApiClient, product_id: int, quantity: int = 1) -> Resp:
    _ = client.request("GET", "/api/cart/")
    return client.request("POST", "/api/cart/", {"product_id": product_id, "quantity": quantity})


def place_checkout(client: ApiClient, extra: dict[str, Any] | None = None, idem_key: str | None = None) -> Resp:
    payload = checkout_payload(**(extra or {}))
    headers = {"Idempotency-Key": idem_key} if idem_key else None
    return client.request("POST", "/api/orders/checkout/", payload, headers)


def extract_order_id(resp: Resp) -> int | None:
    if isinstance(resp.json_data, dict):
        raw = resp.json_data.get("id") or resp.json_data.get("order_id")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None


def create_payment(client: ApiClient, order_id: int) -> Resp:
    return client.request("POST", "/api/orders/payment/", {"order_id": order_id})


def _load_razorpay_secret() -> str | None:
    env_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if env_secret:
        return env_secret

    try:
        import django  # type: ignore

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from django.conf import settings  # type: ignore

        return str(getattr(settings, "RAZORPAY_KEY_SECRET", "") or "") or None
    except Exception as e:
        log(f"Could not load RAZORPAY_KEY_SECRET from Django settings: {e}")
        return None


def _sign(order_id: str, payment_id: str, secret: str) -> str:
    message = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_payment(client: ApiClient, order_id: int, razorpay_order_id: str, razorpay_payment_id: str, signature: str) -> Resp:
    payload = {
        "order_id": order_id,
        "razorpay_order_id": razorpay_order_id,
        "razorpay_payment_id": razorpay_payment_id,
        "razorpay_signature": signature,
    }
    return client.request("POST", "/api/orders/payment/verify/", payload)


def burst(fn, n: int, max_workers: int | None = None) -> list[Resp]:
    out: list[Resp] = []
    workers = max(1, min(max_workers or n, n))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fn, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                out.append(fut.result())
            except Exception as e:  # pragma: no cover (probe resiliency)
                out.append(
                    Resp(
                        method="<worker>",
                        path="<worker>",
                        status=None,
                        body="",
                        json_data=None,
                        headers={},
                        error=f"worker exception: {e}",
                    )
                )
    return out


# -----------------------------
# Probes
# -----------------------------
def probe_a_checkout_burst_full_bodies() -> None:
    section("A) Checkout burst with FULL response bodies")
    c = ApiClient()
    if not ensure_login(c, CUSTOMER1_USERNAME, CUSTOMER1_PASSWORD, CUSTOMER1_EMAIL):
        log("A: skipped (no authenticated customer session).")
        return

    add_resp = ensure_cart_has_item(c, RACE_PRODUCT_ID, 1)
    log_response("A seed cart", add_resp)

    base = c.clone()

    def one(i: int) -> Resp:
        worker = base.clone()
        payload_overrides = {"address": f"221 Probe Street #{i % 2}"}  # keep close to prior probes
        return place_checkout(worker, extra=payload_overrides)

    responses = burst(one, CHECKOUT_BURST_N, max_workers=min(CHECKOUT_BURST_N, 16))

    for idx, resp in enumerate(responses, start=1):
        log_response(f"A[{idx}] checkout", resp)
    summarize_statuses("A", responses)


def probe_b_verify_concurrency_race(secret: str | None) -> None:
    section("B) payment-VERIFY concurrency race (N orders, 1 unit, N verify races)")

    if not secret:
        log("B: skipped (RAZORPAY_KEY_SECRET unavailable).")
        return

    users: list[tuple[str, ApiClient]] = []
    for idx, username in enumerate(VERIFY_USER_CANDIDATES, start=1):
        if len(users) >= VERIFY_RACE_N:
            break
        if not username:
            continue
        client = ApiClient()
        if ensure_login(client, username, VERIFY_PASSWORD, f"{username}@example.com"):
            users.append((username, client))

    if len(users) < 2:
        log(f"B: skipped (need >=2 logged-in users, got {len(users)}).")
        return

    users = users[:VERIFY_RACE_N]
    log(f"B: using users={', '.join(u for u, _ in users)}")

    created: list[tuple[str, ApiClient, int]] = []
    for username, client in users:
        seed = ensure_cart_has_item(client, RACE_PRODUCT_ID, 1)
        log_response(f"B seed cart {username}", seed)
        checkout = place_checkout(client, extra={"address": f"B-Race-{username}"})
        log_response(f"B checkout {username}", checkout)
        oid = extract_order_id(checkout)
        if oid is not None:
            created.append((username, client, oid))

    if len(created) < 2:
        log(f"B: skipped verify race (need >=2 created orders, got {len(created)}).")
        return

    verify_inputs: list[tuple[str, ApiClient, int, str, str, str]] = []
    for username, client, oid in created:
        pay = create_payment(client, oid)
        log_response(f"B payment-create {username} order={oid}", pay)
        if not isinstance(pay.json_data, dict):
            continue
        r_order_id = str(pay.json_data.get("razorpay_order_id") or "")
        if not r_order_id:
            continue
        payment_id = f"pay_redteam_{oid}_{uuid.uuid4().hex[:12]}"
        sig = _sign(r_order_id, payment_id, secret)
        verify_inputs.append((username, client, oid, r_order_id, payment_id, sig))

    if len(verify_inputs) < 2:
        log(f"B: skipped verify race (need >=2 payment intents, got {len(verify_inputs)}).")
        return

    def one(i: int) -> Resp:
        username, client, oid, r_order_id, pay_id, sig = verify_inputs[i]
        resp = verify_payment(client, oid, r_order_id, pay_id, sig)
        log_response(f"B verify {username} order={oid}", resp)
        return resp

    results = burst(one, len(verify_inputs), max_workers=len(verify_inputs))
    summarize_statuses("B", results)


def probe_c_adversarial_inputs(secret: str | None) -> None:
    section("C) Adversarial/malicious input probes")
    c = ApiClient()
    if not ensure_login(c, CUSTOMER1_USERNAME, CUSTOMER1_PASSWORD, CUSTOMER1_EMAIL):
        log("C: skipped (no authenticated customer).")
        return

    neg_qty = c.request("POST", "/api/cart/", {"product_id": RACE_PRODUCT_ID, "quantity": -1})
    log_response("C negative qty", neg_qty)

    overflow_qty = c.request(
        "POST",
        "/api/cart/",
        {"product_id": RACE_PRODUCT_ID, "quantity": "999999999999999999999999999999999999"},
    )
    log_response("C overflow qty", overflow_qty)

    seed = ensure_cart_has_item(c, RACE_PRODUCT_ID, 1)
    log_response("C seed cart for tampered price", seed)
    tampered_payload = checkout_payload(total_amount="0.01", price="0.01", discount_amount="999999")
    tampered_checkout = c.request("POST", "/api/orders/checkout/", tampered_payload)
    log_response("C tampered price fields at checkout", tampered_checkout)

    if not secret:
        log("C replay probe skipped (RAZORPAY_KEY_SECRET unavailable).")
        return

    # Signature replay across different order_id
    # Create two distinct orders (different address to avoid accidental dedup collapse).
    ensure_cart_has_item(c, RACE_PRODUCT_ID, 1)
    o1 = place_checkout(c, extra={"address": "Replay Lane 1"})
    ensure_cart_has_item(c, RACE_PRODUCT_ID, 1)
    o2 = place_checkout(c, extra={"address": "Replay Lane 2"})
    log_response("C replay order-1 create", o1)
    log_response("C replay order-2 create", o2)

    order1 = extract_order_id(o1)
    order2 = extract_order_id(o2)
    if not order1 or not order2:
        log("C replay probe skipped (could not create two orders).")
        return

    p1 = create_payment(c, order1)
    p2 = create_payment(c, order2)
    log_response(f"C replay payment create order={order1}", p1)
    log_response(f"C replay payment create order={order2}", p2)

    if not isinstance(p1.json_data, dict):
        log("C replay probe skipped (payment create for order1 failed).")
        return

    r_order_1 = str(p1.json_data.get("razorpay_order_id") or "")
    if not r_order_1:
        log("C replay probe skipped (order1 has no razorpay_order_id).")
        return

    pay_id = f"pay_replay_{uuid.uuid4().hex[:12]}"
    sig = _sign(r_order_1, pay_id, secret)

    first_verify = verify_payment(c, order1, r_order_1, pay_id, sig)
    replay_cross_order = verify_payment(c, order2, r_order_1, pay_id, sig)

    log_response(f"C replay first verify order={order1}", first_verify)
    log_response(f"C replay cross-order verify order={order2}", replay_cross_order)


def probe_d_idor() -> None:
    section("D) IDOR probe (customer B vs customer A order id)")

    a = ApiClient()
    b = ApiClient()
    ok_a = ensure_login(a, CUSTOMER1_USERNAME, CUSTOMER1_PASSWORD, CUSTOMER1_EMAIL)
    ok_b = ensure_login(b, CUSTOMER2_USERNAME, CUSTOMER2_PASSWORD, CUSTOMER2_EMAIL)
    if not (ok_a and ok_b):
        log("D: skipped (could not authenticate both customer A and customer B).")
        return

    seed = ensure_cart_has_item(a, RACE_PRODUCT_ID, 1)
    log_response("D seed cart A", seed)
    checkout = place_checkout(a, extra={"address": "IDOR A street"})
    log_response("D checkout A", checkout)
    order_id = extract_order_id(checkout)

    if not order_id:
        log("D: skipped (A did not create order).")
        return

    read_try = b.request("GET", f"/api/orders/{order_id}/")
    pay_try = b.request("POST", "/api/orders/payment/", {"order_id": order_id})
    cancel_try = b.request("POST", f"/api/orders/{order_id}/cancel/", {})

    log_response(f"D B GET /api/orders/{order_id}/", read_try)
    log_response(f"D B POST /api/orders/payment/ order_id={order_id}", pay_try)
    log_response(f"D B POST /api/orders/{order_id}/cancel/", cancel_try)

    if cancel_try.status in {404, 405}:
        log("D note: guessed cancel endpoint not found/allowed; skipping this leg per assumption.")


def probe_e_coupon_burst_full_bodies() -> None:
    section("E) Coupon endpoint burst with FULL response bodies")
    c = ApiClient()
    seed = ensure_cart_has_item(c, RACE_PRODUCT_ID, 1)
    log_response("E seed cart", seed)

    base = c.clone()

    def one(i: int) -> Resp:
        worker = base.clone()
        code = f"NOPE-{i:02d}-{uuid.uuid4().hex[:6]}"
        return worker.request("POST", "/api/orders/apply-coupon/", {"code": code})

    responses = burst(one, COUPON_BURST_N, max_workers=min(COUPON_BURST_N, 16))
    for idx, resp in enumerate(responses, start=1):
        log_response(f"E[{idx}] apply-coupon", resp)

    summarize_statuses("E", responses)
    if all(r.status is None for r in responses):
        log("E ALERT: zero tracked HTTP status codes across coupon burst requests.")


def probe_f_auth_session_race() -> None:
    section("F) Auth/session race: concurrent password-recovery token issuance")

    def one(_i: int) -> Resp:
        c = ApiClient()
        return c.request("POST", "/api/accounts/password-reset/", {"email": RECOVERY_EMAIL})

    responses = burst(one, RECOVERY_BURST_N, max_workers=min(RECOVERY_BURST_N, 16))
    for idx, resp in enumerate(responses, start=1):
        log_response(f"F[{idx}] password-reset", resp)

    summarize_statuses("F", responses)


def main() -> int:
    section("red_team_probe.py starting")
    log(f"BASE_URL={BASE_URL}")
    log(f"RACE_PRODUCT_ID={RACE_PRODUCT_ID}")
    log(f"LOG_FILE={LOG_FILE}")

    secret = _load_razorpay_secret()
    log(f"RAZORPAY_KEY_SECRET loaded: {'yes' if secret else 'no'}")

    probes = [
        probe_a_checkout_burst_full_bodies,
        lambda: probe_b_verify_concurrency_race(secret),
        lambda: probe_c_adversarial_inputs(secret),
        probe_d_idor,
        probe_e_coupon_burst_full_bodies,
        probe_f_auth_session_race,
    ]

    failures = 0
    for fn in probes:
        try:
            fn()
        except Exception:  # pragma: no cover (probe resiliency)
            failures += 1
            section(f"Probe crashed: {fn.__name__}")
            log(traceback.format_exc())

    section("red_team_probe.py finished")
    log(f"Probe runner crashes: {failures}")
    log(f"Full log saved to: {LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
