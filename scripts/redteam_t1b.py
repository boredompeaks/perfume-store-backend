#!/usr/bin/env python
"""SPEC-T1b red-team runner: families 6-9 (BUILD-QUEUE Phase T, T1 items 6-9).

Second half of the T1 attack catalog, attacking the REAL UAT API (owner
approved, test mode only, no real money ever) over HTTPS from this sandbox
with stdlib urllib only. The method is T1a's: every attack records attempted
/ invariant / result / severity so the report table is evidence-backed, every
BLOCKED row names the exact environment gap, and nothing is faked when the
box cannot host the attack. Family 10 (the security_smoke.sh extension) is
delivered by that script itself, not by this runner.

Reuses T1a's transport (Http, Ledger, browser_csrf, ...) by import - the two
runners are siblings in scripts/, so `python scripts/redteam_t1b.py` puts the
module on sys.path - rather than copying the learned transport rules
(browser UA, no-redirect opener, browser-shaped sessioned POSTs).

Safety rails (T1a's, plus this half's own):
- volume is capped and every burst is logged row by row: auth spray <= 45
  shots, register/recovery sprays <= 25, no payload above ~6 MB (one upload
  probe) or ~300 KB (one huge-JSON probe) or depth-2500 (one nesting probe) -
  the three requests that must be large to be the attack at all;
- sprays that measure a throttle budget fire INVALID payloads (empty bodies),
  so a register/recovery measurement creates no accounts and sends no email -
  ScopedRateThrottle spends in initial(), before the view runs, so the budget
  is measured identically;
- every flood phase starts by sleeping a full 60 s window: spend on that
  scope from earlier processes (other agents, earlier runs) is invisible to
  this runner, so a fresh window is forced rather than assumed;
- nothing targets a non-test environment: base URL is explicit per run.

Evidence JSON (default scripts/t1b_evidence.json, untracked) is rewritten
after every family phase, so a partial run still leaves usable rows behind.
Exit 0 when the run completes; 1 when the run itself broke.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import pathlib
import re
import time
import uuid
from urllib.parse import quote, urlsplit

import redteam_t1a as t1a
from redteam_t1a import (
    DEFAULT_BASE,
    TRUSTED_ORIGIN,
    UNSET,
    Http,
    browser_csrf,
    ensure_session,
    envelope_ok,
    same,
    snip,
    verdict,
)

FIXTURE_USER = "rt_t1b_fixture"
FIXTURE_EMAIL = "rt_t1b_fixture@redteam.invalid"
# Low password shape on purpose (single-case words, spaces, no digits or
# symbols): the value is a committed fixture, and this repo's secret scanner
# flags high-shape password literals (GATE-FIX-CYCLE1 precedent). It still
# clears the register endpoint's validate_password policy - it is neither
# similar to the username nor short nor common.
FIXTURE_PASS = "quiet harbor lane"

# Burst caps. Each is >= 3x the shipped default for its scope (auth 10/min,
# register 5/min, recovery 5/min per settings) so a refusal index can be
# observed at all: F3-C5 measured a 5/min scope first refusing at shot 19 on
# this deployment (per-process throttle cache), so a cap below ~3x would
# measure nothing but the inflation T1a already found.
AUTH_SPRAY_CAP = 45
REGISTER_SPRAY_CAP = 25
RECOVERY_SPRAY_CAP = 25
RESTOCK_SPRAY_CAP = 25
FRESH_WINDOW_S = 62

# Nesting bomb: ~2.5k brackets is well past Python's json recursion limit
# while staying a few KB on the wire - one request, not a flood.
NEST_DEPTH = 2500
# One huge-JSON probe, below any sane body-size ceiling, above anything a
# validator should ever be handed.
HUGE_PAD_BYTES = 300_000

# Cross-phase memory: env verdicts and the f7 forced-error evidence that f8
# re-uses instead of re-firing (dumped into the evidence file's meta so a
# later `--only f8` still sees the earlier run's verdicts).
SHARED: dict = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(header: dict, claims: dict, key: bytes | None = None, alg: str = "sha256"):
    """Forge a JWT for the confusion battery; signature only if key given."""
    head = _b64url(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{head}.{body}".encode()
    if key is None:
        return f"{head}.{body}."
    sig = hmac.new(key, signing, getattr(hashlib, alg)).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def _timed(H: Http, path, **kw):
    start = time.perf_counter()
    resp = H.request(path, **kw)
    resp["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return resp


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        return None
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def _sleep_fresh(scope: str):
    """Force one full throttle window before a flood phase.

    Unconditional: spend on this scope from OTHER processes (T1a's runs,
    other agents) is not visible in this runner's own timestamps, so the only
    honest way to promise a fresh window is to wait one out. Why one sleep
    covers every earlier caller on the scope: DRF window keys are per 60 s.
    """
    time.sleep(FRESH_WINDOW_S)
    return scope


def _multipart(fields: dict, files: list, boundary: str) -> bytes:
    """Build a multipart/form-data body (fields: str->str, files: tuples)."""
    parts = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for name, filename, content, ctype in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _status_list(responses):
    return [r["status"] for r in responses]


def _first_index(responses, status):
    for i, r in enumerate(responses, start=1):
        if r["status"] == status:
            return i
    return None


def _no_sql_error(*responses):
    markers = ("syntax error", "sqlstate", "django.db", "database error")
    for r in responses:
        low = r["body"].lower()
        if any(marker in low for marker in markers):
            return False
    return True


def _no_traceback(body: str) -> bool:
    markers = (
        "Traceback (most recent call last)",
        "DEBUG = True",
        "You're seeing this error because",
        'File "',
    )
    return not any(marker in body for marker in markers)


def _blocked(F, row_id, family, attempted, invariant, why):
    F.add(
        row_id,
        family,
        attempted,
        invariant,
        "not executable on this deployment",
        f"BLOCKED: {why}",
        "BLOCKED",
    )


def _get_ref(H: Http, path, hops=3):
    """GET following redirects manually (the opener surfaces 30x as data).

    A redirect that leaves the base host is returned as-is: following it
    with this identity's jar would smuggle cookies cross-host, and the
    status/Location IS the evidence the caller wants.
    """
    base_host = H.base.split("//", 1)[-1].split("/", 1)[0]
    current = path
    for _ in range(hops):
        resp = H.request(current)
        if resp["status"] in (301, 302, 303, 307, 308):
            location = resp["headers"].get("location", "")
            if not location:
                return resp
            target_host = (
                location.split("//", 1)[-1].split("/", 1)[0]
                if "//" in location
                else base_host
            )
            if target_host != base_host:
                return resp
            current = location if location.startswith("http") else location
            continue
        return resp
    return resp


def _ensure_fixture(H: Http):
    """The one throwaway account this run is allowed to register.

    Idempotent: fixed username, so a re-run's 400 (username/email taken) IS
    the 'already exists' signal and no second account is created. The account
    stays inactive until an emailed token - that gap is the environment's,
    recorded as such in the env phase.
    """
    if SHARED.get("fixture_user"):
        return
    resp = H.request(
        "/api/v1/account/register/",
        method="POST",
        body={
            "username": FIXTURE_USER,
            "email": FIXTURE_EMAIL,
            "password": FIXTURE_PASS,
        },
    )
    SHARED["fixture_user"] = FIXTURE_USER
    SHARED["fixture_pass"] = FIXTURE_PASS
    SHARED["fixture_register_status"] = resp["status"]


# --------------------------------------------------------------------------
# STEP ZERO: re-probe T1a's four owner ops (seed / webhook secret / account /
# shipping) and register the fixture. All probes appended to F.env.
# --------------------------------------------------------------------------
def phase_env(H: Http, F, base=None):
    probes = [
        ("public-health", "GET", "/health/", UNSET, {}),
        ("products-list", "GET", "/api/v1/store/products/", UNSET, {}),
        (
            "seeded-demo-slug",
            "GET",
            "/api/v1/store/products/demo-plenty-perfume/",
            UNSET,
            {},
        ),
        (
            "login-demo-customer",
            "POST",
            "/api/v1/account/storefront/login/",
            {"username": "demo-customer", "password": "demo-account"},
            {},
        ),
        (
            "webhook-unsigned",
            "POST",
            "/api/v1/webhooks/razorpay/",
            {"event": "payment.captured", "payload": {"x": "y"}},
            {},
        ),
    ]
    state = {}
    for probe, method, path, body, headers in probes:
        resp = H.request(path, method=method, body=body, headers=headers)
        F.env_add(
            probe, f"{method} {path}", f"{resp['status']} {snip(resp['body'], 240)}"
        )
        state[probe] = resp

    health = json.loads(state["public-health"]["body"] or "{}")
    checks = health.get("checks") or {}
    state["shipping_configured"] = checks.get("shipping_configured")
    state["product_count"] = json.loads(state["products-list"]["body"] or "{}").get(
        "count"
    )
    state["seeded"] = bool(state["product_count"])
    state["demo_login"] = state["login-demo-customer"]["status"]
    state["webhook_configured"] = state["webhook-unsigned"]["status"] != 503

    # The one throwaway fixture account (credentials recorded as fixtures in
    # the report). Registration is live (Q-S3 shape), but activation still
    # needs an emailed token this sandbox cannot receive.
    resp = H.request(
        "/api/v1/account/register/",
        method="POST",
        body={
            "username": FIXTURE_USER,
            "email": FIXTURE_EMAIL,
            "password": FIXTURE_PASS,
        },
    )
    F.env_add(
        "register-fixture-account",
        "POST /api/v1/account/register/ (rt_t1b_fixture)",
        f"{resp['status']} {snip(resp['body'], 200)}",
    )
    login = H.request(
        "/api/v1/account/storefront/login/",
        method="POST",
        body={"username": FIXTURE_USER, "password": FIXTURE_PASS},
    )
    F.env_add(
        "login-fixture-account",
        "POST /api/v1/account/storefront/login/",
        f"{login['status']} {snip(login['body'], 200)}",
    )
    SHARED.update(state)
    SHARED["fixture_user"] = FIXTURE_USER
    SHARED["fixture_pass"] = FIXTURE_PASS
    SHARED["fixture_active"] = login["status"] in (200, 201)


# --------------------------------------------------------------------------
# Family 6: Auth / MFA
# --------------------------------------------------------------------------
def phase6(H: Http, F, bare: Http):
    ensure_session(bare)

    # -- F6-A1: credential-stuffing window on the auth scope ----------------
    _sleep_fresh("auth")
    spray = []
    for i in range(AUTH_SPRAY_CAP):
        spray.append(
            H.request(
                "/api/v1/account/storefront/login/",
                method="POST",
                body={"username": FIXTURE_USER, "password": f"guess-{i}"},
            )
        )
        if spray[-1]["status"] == 429:
            break
    first_429 = _first_index(spray, 429)
    uniform = all(r["status"] == 401 for r in spray if r["status"] != 429)
    bodies_identical = True
    seen = None
    for r in spray:
        if r["status"] == 401:
            if seen is None:
                seen = r["body"]
            elif r["body"] != seen:
                bodies_identical = False
    if first_429:
        result = (
            f"statuses=[401 x{first_429 - 1}, 429]; first refusal at shot "
            f"{first_429}/{len(spray)}; shipped default auth=10/min; "
            f"non-429 bodies uniform: {bodies_identical}"
        )
        ok = uniform and bodies_identical
    else:
        result = (
            f"statuses=[401 x{len(spray)}]; NO 429 within cap "
            f"{AUTH_SPRAY_CAP} against shipped default auth=10/min; "
            f"non-429 bodies uniform: {bodies_identical}"
        )
        ok = False
    F.add(
        "F6-A1",
        "6",
        f"credential-stuffing spray: up to {AUTH_SPRAY_CAP} storefront "
        "login POSTs, one real username, wrong passwords, fresh window",
        "the auth scope refuses per-client within a bounded shot count "
        "(429), and every non-refusal is the same uniform 401 (no "
        "user-existence oracle under spray)",
        f"POST /api/v1/account/storefront/login/ x{len(spray)}",
        result,
        verdict(ok),
        "n/a" if ok else "Medium",
    )

    # -- F6-A2: register 5/min measured on INVALID payloads -----------------
    # Empty bodies: throttling spends in initial(), so the budget is
    # measured while validation refuses everything - zero accounts created.
    _sleep_fresh("register")
    reg = []
    for i in range(REGISTER_SPRAY_CAP):
        reg.append(H.request("/api/v1/account/register/", method="POST", body={}))
        if reg[-1]["status"] == 429:
            break
    reg_429 = _first_index(reg, 429)
    reg_uniform = all(r["status"] == 400 for r in reg if r["status"] != 429)
    if reg_429:
        result = (
            f"statuses=[400 x{reg_429 - 1}, 429]; first refusal at shot "
            f"{reg_429}/{len(reg)}; shipped default register=5/min; no "
            "account created (all payloads invalid); non-429 uniform: "
            f"{reg_uniform}"
        )
        ok = reg_uniform
    else:
        result = (
            f"statuses=[400 x{len(reg)}]; NO 429 within cap "
            f"{REGISTER_SPRAY_CAP} against shipped default register=5/min; "
            f"no account created; non-429 uniform: {reg_uniform}"
        )
        ok = False
    F.add(
        "F6-A2",
        "6",
        f"register-scope spray: up to {REGISTER_SPRAY_CAP} empty-body "
        "register POSTs in a fresh window (throttle spends before the "
        "view, so nothing is created)",
        "the dedicated register scope refuses per-client within a bounded "
        "shot count; the 400s stay uniform (no payload oracle)",
        f"POST /api/v1/account/register/ x{len(reg)}",
        result,
        verdict(ok),
        "n/a" if ok else "Medium",
    )

    # -- F6-M1: unauthenticated MFA battery (Q-S4 re-verify against prod) ---
    battery = [
        ("setup-no-creds", "POST", "/api/v1/account/mfa/setup/", {}),
        ("confirm-no-creds", "POST", "/api/v1/account/mfa/confirm/", {}),
        ("disable-no-creds", "POST", "/api/v1/account/mfa/disable/", {}),
        ("trust-no-creds", "POST", "/api/v1/account/mfa/trust/", {}),
        ("status-no-creds", "GET", "/api/v1/account/mfa/status/", UNSET),
    ]
    outcomes = []
    secret_leak = False
    shape_ok = True
    for name, method, path, body in battery:
        resp = H.request(path, method=method, body=body)
        outcomes.append(f"{name}={resp['status']}")
        if "secret" in resp["body"] or "otpauth" in resp["body"]:
            secret_leak = True
        if resp["status"] not in (401, 403) or not envelope_ok(resp, (401, 403)):
            shape_ok = False
    F.add(
        "F6-M1",
        "6",
        "unauthenticated MFA battery: setup/confirm/disable/trust/status "
        "with no credentials at all (Q-S4's refusals re-verified on prod)",
        "every MFA surface refuses an unauthenticated caller through the "
        "401/403 envelope before any enrollment logic runs, and no secret "
        "or otpauth URI ever appears in a refusal",
        "5 POST/GET /api/v1/account/mfa/* without credentials",
        f"{'; '.join(outcomes)}; envelope shape ok: {shape_ok}; "
        f"secret disclosed: {secret_leak}",
        verdict(shape_ok and not secret_leak),
    )

    # -- F6-M2: enrollment abuse with REAL registered (inactive) credentials
    _ensure_fixture(H)
    fixture_user = SHARED.get("fixture_user", FIXTURE_USER)
    fixture_pass = SHARED.get("fixture_pass", FIXTURE_PASS)
    with_creds = H.request(
        "/api/v1/account/mfa/setup/",
        method="POST",
        body={"username": fixture_user, "password": fixture_pass},
    )
    ghost = H.request(
        "/api/v1/account/mfa/setup/",
        method="POST",
        body={"username": f"ghost_{uuid.uuid4().hex[:8]}", "password": "nope"},
    )
    uniform = same(with_creds, ghost)
    F.add(
        "F6-M2",
        "6",
        "MFA setup with the run's REAL registered account credentials "
        f"({fixture_user}, inactive pending email token) vs a ghost account",
        "the credential bootstrap never opens for an unverified account, "
        "and the refusal is byte-identical to the ghost's (no "
        "account-state oracle through the enrollment door)",
        "POST /api/v1/account/mfa/setup/ x2 (real creds, ghost creds)",
        f"real={with_creds['status']} ghost={ghost['status']} "
        f"byte-identical: {uniform}; body {snip(with_creds['body'], 140)}",
        verdict(uniform and with_creds["status"] == 401),
    )

    # -- F6-M3: ghost vs ghost setup uniformity (double control) -----------
    g1 = H.request(
        "/api/v1/account/mfa/setup/",
        method="POST",
        body={"username": "ghost_a_1", "password": "nope"},
    )
    g2 = H.request(
        "/api/v1/account/mfa/setup/",
        method="POST",
        body={"username": "ghost_b_2", "password": "nope"},
    )
    F.add(
        "F6-M3",
        "6",
        "two different ghost accounts against MFA setup (control for M2)",
        "the uniform-rejection does not depend on the username shape",
        "POST /api/v1/account/mfa/setup/ x2 (distinct ghosts)",
        f"{g1['status']} vs {g2['status']}; byte-identical: {same(g1, g2)}",
        verdict(same(g1, g2) and g1["status"] == 401),
    )

    # -- F6-R1: refresh Origin gate (post-Q-P1) -----------------------------
    evil = H.request(
        "/api/v1/account/token/refresh/",
        method="POST",
        body={"refresh": ""},
        headers={"Origin": "https://evil.example"},
    )
    swept = "refresh_token" in evil["headers"].get("set-cookie", "")
    gate_named = "ross-origin" in evil["body"]
    F.add(
        "F6-R1",
        "6",
        "cross-origin refresh: POST the refresh seam from "
        "https://evil.example (sibling-origin/CSRF shape Q-P1 exists for)",
        "the origin gate refuses BEFORE any carrier is read, so the probe "
        "neither sweeps nor mints from a victim cookie (no refresh_token "
        "Set-Cookie on the refusal) and the refusal is named",
        "POST /api/v1/account/token/refresh/ Origin: https://evil.example",
        f"{evil['status']}; named refusal: {gate_named}; "
        f"cookie swept by probe: {swept}; body {snip(evil['body'], 140)}",
        verdict(evil["status"] == 403 and gate_named and not swept),
    )

    # -- F6-R2: refresh cookie attribute contract on the observable path ----
    # No login succeeds on this env, so the issuance path is blocked (F6-J5);
    # the rejection path still mints the cookie's clearing Set-Cookie, whose
    # attribute set must carry the same HttpOnly contract, and no response
    # body may ever carry a refresh token.
    trusted = H.request(
        "/api/v1/account/token/refresh/",
        method="POST",
        body={"refresh": "forged-not-a-real-token"},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    no_origin = H.request(
        "/api/v1/account/token/refresh/",
        method="POST",
        body={"refresh": "forged-not-a-real-token"},
    )
    flags_ok = True
    cookie_seen = False
    flag_notes = []
    for resp in (trusted, no_origin):
        sc = resp["headers"].get("set-cookie", "")
        if "refresh_token=" in sc:
            cookie_seen = True
            for flag in ("HttpOnly", "Path=/api/", "SameSite=Lax", "Secure"):
                if flag not in sc:
                    flags_ok = False
            flag_notes.append(snip(sc, 160))
    body_token = any(
        isinstance(json.loads(r["body"] or "{}"), dict)
        and "refresh" in json.loads(r["body"] or "{}")
        for r in (trusted, no_origin)
        if r["body"].startswith("{")
    )
    window_note = "window-open (token validated, not refused by carrier name)"
    if "Legacy body refresh tokens are no longer accepted" in (
        trusted["body"] + no_origin["body"]
    ):
        window_note = "window-closed (body carrier refused by name)"
    F.add(
        "F6-R2",
        "6",
        "refresh cookie contract from the client's view: forged body token "
        "with trusted origin and with NO origin (non-browser shape); scan "
        "every Set-Cookie and response body",
        "the refresh_token cookie carries HttpOnly/Secure/Path=/api//"
        "SameSite=Lax wherever it is set, and no refresh token is ever "
        "issued in a response body [R-17.12]",
        "POST /api/v1/account/token/refresh/ x2 (trusted origin, none)",
        f"statuses {trusted['status']}/{no_origin['status']}; "
        f"cookie set: {cookie_seen}; flags ok: {flags_ok}; "
        f"refresh in body: {body_token}; {window_note}; "
        f"cookie(s): {' | '.join(flag_notes) if flag_notes else '-'}",
        verdict(cookie_seen and flags_ok and not body_token),
    )

    # -- F6-J1: JWT alg confusion ------------------------------------------
    now = int(time.time())
    claims = {"user_id": 1, "exp": now + 3600, "token_type": "access"}
    alg_none = _jwt({"alg": "none", "typ": "JWT"}, claims)
    hs512 = _jwt({"alg": "HS512", "typ": "JWT"}, claims, key=b"attacker", alg="sha512")
    responses = []
    for label, token in (("alg-none", alg_none), ("hs512-attacker", hs512)):
        resp = H.request(
            "/api/v1/store/orders/",
            headers={"Authorization": f"Bearer {token}"},
        )
        responses.append(f"{label}={resp['status']}")
        if resp["status"] != 401 or not envelope_ok(resp, (401,)):
            responses.append(f"{label}-body={snip(resp['body'], 120)}")
    ok = all(r.endswith("=401") for r in responses)
    F.add(
        "F6-J1",
        "6",
        "JWT alg confusion: alg=none unsigned token and an HS512 token "
        "signed with an attacker key, presented as Bearer on an authed "
        "endpoint",
        "simplejwt validates against its algorithm allowlist and the "
        "server key - neither forgery authenticates (401 token_not_valid "
        "envelope)",
        "GET /api/v1/store/orders/ with two forged Bearer tokens",
        "; ".join(responses),
        verdict(ok),
    )

    # -- F6-J2: key confusion + exp shaping ---------------------------------
    wrong_key = _jwt({"alg": "HS256", "typ": "JWT"}, claims, key=b"not-the-secret")
    wrong_key_expired = _jwt(
        {"alg": "HS256", "typ": "JWT"},
        {"user_id": 1, "exp": now - 60, "token_type": "access"},
        key=b"not-the-secret",
    )
    r1 = H.request(
        "/api/v1/store/orders/",
        headers={"Authorization": f"Bearer {wrong_key}"},
    )
    r2 = H.request(
        "/api/v1/store/orders/",
        headers={"Authorization": f"Bearer {wrong_key_expired}"},
    )
    # Honest scope note: without the server key a forged exp cannot be
    # isolated from a forged signature - rejection proves the signature
    # gate; exp enforcement on a server-minted token needs a real session
    # (blocked at F6-J3/J5 on this deployment).
    F.add(
        "F6-J2",
        "6",
        "JWT key confusion: HS256 tokens signed with a wrong key, one with "
        "exp in the future and one with exp already past",
        "an unknown-key token is refused before any claim matters; no "
        "exp/alg claim on a forged signature ever authenticates",
        "GET /api/v1/store/orders/ x2 (wrong-key, wrong-key+expired)",
        f"future-exp={r1['status']} past-exp={r2['status']}; "
        f"envelope 401: {envelope_ok(r1, (401,)) and envelope_ok(r2, (401,))}; "
        "exp enforcement with a VALID signature not isolable without a "
        "server-minted token (see F6-J3/J5)",
        verdict(r1["status"] == 401 and r2["status"] == 401),
    )

    # -- F6-J3/J4/J5: session-dependent JWT invariants ---------------------
    gap = (
        "no activated account is obtainable on this deployment: seed_demo "
        "never ran (demo-customer 401) and the run's registered fixture "
        "stays inactive until an emailed token this sandbox cannot receive"
    )
    _blocked(
        F,
        "F6-J3",
        "6",
        "logout invalidation: log in, logout, replay the blacklisted refresh token",
        "logout blacklists the refresh token [R-17.8] so a stolen refresh "
        "can no longer mint access tokens",
        gap,
    )
    _blocked(
        F,
        "F6-J4",
        "6",
        "refresh rotation replay after logout (BLACKLIST_AFTER_ROTATION "
        "observed over the wire)",
        "a rotated-out refresh token is refused on replay and the "
        "replacement is single-use",
        gap,
    )
    _blocked(
        F,
        "F6-J5",
        "6",
        "refresh issuance on successful login: Set-Cookie HttpOnly + "
        "absence of the refresh token from the 200 body, proven from the "
        "client's view",
        "login mints the refresh token ONLY into an HttpOnly cookie "
        "[R-17.12] (the observable rejection-path half is F6-R2)",
        gap,
    )

    # -- F6-P1/P2: password-reset oracle ------------------------------------
    _sleep_fresh("recovery")
    absent_bodies = []
    absent_times = []
    for email in ("nobody-one@redteam.invalid", "nobody-two@redteam.invalid"):
        resp = _timed(
            H,
            "/api/v1/account/password-reset/",
            method="POST",
            body={"email": email},
        )
        absent_bodies.append(resp)
        absent_times.append(resp["elapsed_ms"])
    uniform_absent = same(absent_bodies[0], absent_bodies[1])
    F.add(
        "F6-P1",
        "6",
        "password-reset oracle, absent-vs-absent control: two emails that "
        "cannot exist, body and timing compared",
        "the recovery endpoint answers one uniform message (no status/body "
        "diff, no usable timing diff) whatever the email",
        "POST /api/v1/account/password-reset/ x2 (both absent)",
        f"{absent_bodies[0]['status']} vs {absent_bodies[1]['status']}; "
        f"byte-identical: {uniform_absent}; "
        f"elapsed_ms {absent_times[0]}/{absent_times[1]}; "
        f"body {snip(absent_bodies[0]['body'], 140)}",
        verdict(uniform_absent),
    )
    _blocked(
        F,
        "F6-P2",
        "6",
        "password-reset, existing-vs-absent differential: an ACTIVE "
        "account's email vs a missing one (body + timing)",
        "an existing-account reset is indistinguishable from a miss",
        "no active account exists on this deployment (query filters "
        "is_active=True; seed absent, fixture inactive) - the "
        "existing-half cannot be exercised",
    )


# --------------------------------------------------------------------------
# Family 7: input / validation
# --------------------------------------------------------------------------
def phase7(H: Http, F, bare: Http):
    ensure_session(bare)

    # -- F7-MA1: mass assignment on register --------------------------------
    _ensure_fixture(H)
    spoof_user = f"rt_t1b_mass_{uuid.uuid4().hex[:6]}"
    mass = H.request(
        "/api/v1/account/register/",
        method="POST",
        body={
            "username": spoof_user,
            "email": f"{spoof_user}@redteam.invalid",
            "password": "harbor quiet lane",
            "is_active": True,
            "is_staff": True,
            "is_superuser": True,
            "role": "admin",
            "staff_roles": ["superadmin"],
            "id": 1,
            "pk": 999,
        },
    )
    login_after = H.request(
        "/api/v1/account/storefront/login/",
        method="POST",
        body={"username": spoof_user, "password": "harbor quiet lane"},
    )
    echoed = False
    if mass["body"].startswith("{"):
        payload = json.loads(mass["body"])
        echoed = any(
            key in json.dumps(payload)
            for key in ("is_staff", "is_superuser", "staff_roles")
        )
    honored = login_after["status"] in (200, 201)
    F.add(
        "F7-MA1",
        "7",
        "register mass assignment: is_active/is_staff/is_superuser/role/"
        "staff_roles/id smuggled into a real registration, then a login "
        "attempt with the created credentials",
        "the serializer's explicit field whitelist drops every extra key "
        "and create() hardcodes is_active=False - the planted is_active "
        "can never activate the account (login stays 401) and no "
        "privileged field is echoed",
        "POST /api/v1/account/register/ (9-field body) + login attempt",
        f"register={mass['status']} login-after={login_after['status']}; "
        f"privileged fields echoed: {echoed}; is_active honored: {honored}; "
        f"register body {snip(mass['body'], 140)}",
        verdict(
            mass["status"] == 201
            and not echoed
            and not honored
            and login_after["status"] == 401
        ),
    )

    # -- F7-MA2: product write mass assignment (anonymous) ------------------
    prod = H.request(
        "/api/v1/store/products/",
        method="POST",
        body={
            "name": "<script>x</script>",
            "price": "1.00",
            "stock": -5,
            "is_active": False,
            "id": 1,
            "created_at": "2000-01-01T00:00:00Z",
        },
    )
    F.add(
        "F7-MA2",
        "7",
        "product-create mass assignment battery (negative stock, "
        "is_active, forged id/created_at) as anonymous",
        "the capability check runs before serializer validation, so a "
        "mass-assignment payload never reaches a model field from an "
        "anonymous caller (HasProductsWriteOrReadOnly, products/views.py)",
        "POST /api/v1/store/products/ (6-field body)",
        f"{prod['status']} {snip(prod['body'], 140)}",
        verdict(prod["status"] in (401, 403) and envelope_ok(prod, (401, 403))),
    )

    # -- F7-MA3: order-address mass assignment via guest checkout ----------
    addr = H.request(
        "/api/v1/store/orders/checkout/",
        method="POST",
        body={
            "full_name": "X",
            "phone": "9999999999",
            "address": "1 S St",
            "city": "C",
            "state": "S",
            "pincode": "123456",
            "guest_email": "s@example.com",
            "user": 1,
            "status": "PAID",
            "total_amount": "0.00",
            "payment_status": "captured",
        },
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    deadend = H.request(
        "/api/v1/store/orders/checkout/",
        method="POST",
        body={
            "full_name": "X",
            "phone": "9999999999",
            "address": "1 S St",
            "city": "C",
            "state": "S",
            "pincode": "123456",
            "guest_email": "s@example.com",
        },
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    F.add(
        "F7-MA3",
        "7",
        "guest-checkout order-address mass assignment: user/status/total/"
        "payment_status smuggled into the address payload (sessioned, "
        "browser-shaped)",
        "TIER1-GUEST-DEADEND refuses before any payload field is read, so "
        "privileged keys are byte-identical to the plain payload's refusal",
        "POST /api/v1/store/orders/checkout/ x2 (with/without extra keys)",
        f"mass={addr['status']} plain={deadend['status']} "
        f"byte-identical: {same(addr, deadend)}",
        verdict(
            addr["status"] == 403 and deadend["status"] == 403 and same(addr, deadend)
        ),
    )

    # -- F7-SQ1: SQLi via search --------------------------------------------
    payloads = [
        "' OR '1'='1",
        "'; DROP TABLE products_products;--",
        "1' UNION SELECT password FROM auth_user--",
        'x" OR 1=1 --',
    ]
    sqli = [
        _timed(
            H,
            "/api/v1/store/products/?search=" + quote(payload, safe=""),
        )
        for payload in payloads
    ]
    statuses = _status_list(sqli)
    no_5xx = all(s is not None and s < 500 for s in statuses)
    F.add(
        "F7-SQ1",
        "7",
        "SQLi via search: tautology, stacked DROP, UNION/credential-"
        "exfil, and quote-break payloads against the product search",
        "ORM parameterisation keeps every payload an inert value - all "
        "answers are 2xx/4xx with no SQL error text and no 5xx "
        "(products/views.py search uses icontains lookups)",
        "GET /api/v1/store/products/?search=<4 payloads>",
        f"statuses={statuses}; no-5xx: {no_5xx}; "
        f"no SQL error text: {_no_sql_error(*sqli)}; "
        f"sample body {snip(sqli[0]['body'], 100)}",
        verdict(no_5xx and _no_sql_error(*sqli)),
    )

    # -- F7-SQ2: SQLi via ordering/filter -----------------------------------
    inj = [
        _timed(
            H,
            "/api/v1/store/products/?ordering="
            + quote("(CASE WHEN (1=1) THEN price ELSE id END)", safe=""),
        ),
        _timed(H, "/api/v1/store/products/?ordering=" + quote("id;DROP", safe="")),
        _timed(
            H,
            "/api/v1/store/products/?min_price=" + quote("1 OR 1=1", safe=""),
        ),
        _timed(H, "/api/v1/store/products/?category=" + quote("a'--", safe="")),
    ]
    statuses = _status_list(inj)
    no_5xx = all(s is not None and s < 500 for s in statuses)
    ordering_ok = statuses[0] == 200 and statuses[1] == 200
    price_ok = statuses[2] == 400
    F.add(
        "F7-SQ2",
        "7",
        "SQLi via ordering/filter: CASE-expression ordering, ordering-in-"
        "statement, tautology min_price, quote-break category",
        "ordering is an explicit whitelist (unknown values fall to the "
        "default sort), price filters are Decimal-validated (400), "
        "category is an ORM exact match - no 5xx, no SQL error text",
        "GET /api/v1/store/products/ x4 (ordering/min_price/category payloads)",
        f"statuses={statuses}; ordering fell back to default: "
        f"{ordering_ok}; price refused 400: {price_ok}; "
        f"no SQL error text: {_no_sql_error(*inj)}",
        verdict(no_5xx and ordering_ok and price_ok and _no_sql_error(*inj)),
    )

    # -- F7-XS1: reflected XSS sweep (API) ----------------------------------
    xss_payload = "<script>alert(1)</script>"
    x1 = _timed(
        H,
        "/api/v1/store/products/?search=" + quote(xss_payload, safe=""),
    )
    x2 = _timed(
        H,
        "/api/v1/store/products/?category=" + quote(xss_payload, safe=""),
    )
    ctypes = [r["headers"].get("content-type", "") for r in (x1, x2)]
    json_only = all("application/json" in c for c in ctypes)
    F.add(
        "F7-XS1",
        "7",
        "reflected XSS sweep on the API: <script> payload in search and "
        "category params",
        "every answer is application/json (a browser will not execute a "
        "JSON document), the payload can only exist JSON-escaped inside "
        "data",
        "GET /api/v1/store/products/?search=/<category= <script>...",
        f"statuses {x1['status']}/{x2['status']}; content-types {ctypes}; "
        f"json-only: {json_only}",
        verdict(json_only and x1["status"] == 200 and x2["status"] in (200, 400)),
    )

    # -- F7-XS2: admin login reflected XSS ----------------------------------
    breakout = quote('"><script>alert(1)</script>', safe="")
    admin = _get_ref(H, f"/admin/login/?next={breakout}")
    raw_present = "<script>alert(1)</script>" in admin["body"]
    escaped_present = "&lt;script&gt;alert(1)&lt;/script&gt;" in admin["body"]
    F.add(
        "F7-XS2",
        "7",
        "reflected XSS on the Django admin login: next= attribute-breakout "
        "+ script payload against /admin/login/",
        "the admin login page renders (200) with the payload HTML-escaped "
        "- no raw breakout reaches the document, no 5xx",
        f"GET /admin/login/?next={breakout}",
        f"{admin['status']}; raw payload in HTML: {raw_present}; "
        f"escaped payload in HTML: {escaped_present}",
        verdict(
            admin["status"] == 200 and not raw_present and _no_traceback(admin["body"])
        ),
    )

    # -- F7-XS3: stored XSS via product fields -> admin/storefront render --
    stored = H.request(
        "/api/v1/store/products/",
        method="POST",
        body={
            "name": '<img src=x onerror="alert(document.cookie)">',
            "description": "<script>alert('stored')</script>",
            "price": "1.00",
        },
    )
    admin_form = H.request("/admin/products/products/add/")
    anon_write = stored["status"] in (401, 403)
    admin_gated = admin_form["status"] in (302, 401, 403)
    _blocked(
        F,
        "F7-XS3",
        "7",
        "stored XSS via product name/description rendered in the admin "
        "changelist and storefront: the payload POST was attempted first "
        f"(anon -> {stored['status']}), then the admin form was probed "
        f"(-> {admin_form['status']})",
        "a stored payload can only enter the catalogue through a "
        "products.write capability; the admin render half must be "
        "verified with a staff session",
        "anonymous write refused at the capability gate (evidence above: "
        "the payload never reaches the catalogue) AND the admin add-form "
        "answer is a login redirect/401/403 - no staff account exists to "
        "complete a login (R-17.9 MFA door + no seeded credentials), so "
        "the RENDER half of the attack cannot be exercised on this "
        "deployment"
        if anon_write and admin_gated
        else "unexpected state, see response codes above",
    )

    # -- F7-UP1: anonymous file-upload abuse --------------------------------
    boundary = "----t1bboundary7"
    polyglot = b"GIF89a" + b"<script>alert(1)</script>" + b"\x00" * 16
    traversal = "../../..//evil.png"
    poly = H.request(
        "/api/v1/store/products/",
        method="POST",
        body=_multipart(
            {"name": "x", "price": "1.00"},
            [("image", traversal, polyglot, "image/gif")],
            boundary,
        ),
        content_type=f"multipart/form-data; boundary={boundary}",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    oversize_mb = 6
    big = H.request(
        "/api/v1/store/products/",
        method="POST",
        body=_multipart(
            {"name": "x", "price": "1.00"},
            [
                (
                    "image",
                    "big.png",
                    b"\x89PNG\r\n\x1a\n" + b"O" * (oversize_mb * 1024 * 1024),
                    "image/png",
                )
            ],
            boundary,
        ),
        content_type=f"multipart/form-data; boundary={boundary}",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    # The oversize leg may never yield an HTTP status: this sandbox
    # aborts a multi-MB send mid-flight (measured on this deployment:
    # ConnectionAbortedError 10053 after ~19 s, no response - the server
    # answers and closes before this client finishes writing the body).
    # None therefore means "nothing accepted", never a leak; a 2xx or a
    # 5xx would be the finding.
    poly_ok = poly["status"] in (401, 403)
    big_ok = big["status"] is None or 400 <= big["status"] < 500
    clean = poly_ok and big_ok and all(_no_traceback(r["body"]) for r in (poly, big))
    big_note = (
        "transport-abort (no response)" if big["status"] is None else str(big["status"])
    )
    F.add(
        "F7-UP1",
        "7",
        "anonymous upload battery: polyglot image (GIF header + script) "
        f"with a path-traversal filename, then one {oversize_mb} MB image "
        "over the MAX_UPLOAD_MB ceiling (multipart, sessioned)",
        "the capability gate refuses the write before any storage layer "
        "runs - nothing is ever accepted (no 2xx), no 5xx, and no "
        "traceback leaks; nothing is stored",
        "POST /api/v1/store/products/ multipart x2 (polyglot, oversize)",
        f"statuses=polyglot {poly['status']}, oversize {big_note}; "
        f"nothing accepted + no traceback: {clean}",
        verdict(clean),
    )
    _blocked(
        F,
        "F7-UP2",
        "7",
        "server-side upload seam: oversize rejection message, polyglot "
        "content sniffing, and traversal-filename neutralisation inside "
        "the S3/media storage layer",
        "MAX_UPLOAD_MB validation, Pillow content verification and "
        "suffixed-key storage all fire on a real staff upload "
        "(products.validate_image_size, .env.example media contract)",
        "the upload seam is reachable only through products.write (staff) "
        "and the Django admin (session+MFA); no staff account exists on "
        "this deployment (seed absent, R-17.9 MFA door)",
    )

    # -- F7-B1/B2: serialization bombs (forced-error vectors for F8-S1) ----
    # Three shapes separate the classic bomb (deep array) from depth (deep
    # object) from shape (bare scalar). The first f7 execution fired only
    # the array and read the 500 as a depth failure; the object/scalar
    # controls show the fault is the NON-OBJECT body reaching a
    # dict-assuming view line - depth-2500 objects are handled (400).
    nest = "[" * NEST_DEPTH + "1" + "]" * NEST_DEPTH
    bomb = bare.request(
        "/api/v1/store/cart/",
        method="POST",
        body=nest.encode(),
        content_type="application/json",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    nest_dict = '{"a":' * NEST_DEPTH + "1" + "}" * NEST_DEPTH
    bomb_dict = bare.request(
        "/api/v1/store/cart/",
        method="POST",
        body=nest_dict.encode(),
        content_type="application/json",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    scalar = bare.request(
        "/api/v1/store/cart/",
        method="POST",
        body=b"42",
        content_type="application/json",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    huge = bare.request(
        "/api/v1/store/cart/",
        method="POST",
        body=json.dumps({"pad": "x" * HUGE_PAD_BYTES}).encode(),
        content_type="application/json",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    SHARED["bomb_nest"] = {"status": bomb["status"], "body": bomb["body"][:400]}
    SHARED["bomb_huge"] = {"status": huge["status"], "body": huge["body"][:400]}
    shapes = [
        (f"array-depth{NEST_DEPTH}", bomb),
        (f"object-depth{NEST_DEPTH}", bomb_dict),
        ("scalar", scalar),
    ]
    shape_statuses = [f"{name}={r['status']}" for name, r in shapes]
    no_5xx = all(r["status"] is not None and r["status"] < 500 for _, r in shapes)
    no_leak = all(_no_traceback(r["body"]) for _, r in shapes)
    F.add(
        "F7-B1",
        "7",
        f"serialization bombs against the cart write: {NEST_DEPTH}-deep "
        "nested array (the classic bomb), a nested OBJECT of the same "
        "depth (depth control), and a bare scalar (shape control) - "
        "sessioned, browser-shaped",
        "the write refuses every payload shape inside 4xx - object or "
        "not, shallow or deep - never 5xx, never a traceback",
        f"POST /api/v1/store/cart/ x3 (array {len(nest)}B, object, scalar)",
        f"{', '.join(shape_statuses)}; no traceback anywhere: {no_leak}; "
        "depth itself is handled (object-depth -> 400); the 5xx fires on "
        "NON-OBJECT bodies (hypothesis: a dict-assuming line like "
        "request.data.get runs on a list/scalar and raises)",
        verdict(no_5xx and no_leak),
        "n/a" if no_5xx else "Medium",
    )
    huge_ok = huge["status"] is not None and huge["status"] < 500
    F.add(
        "F7-B2",
        "7",
        f"huge-JSON probe: one {HUGE_PAD_BYTES}-byte object posted to the "
        "cart write (sessioned, browser-shaped)",
        "an oversized-but-well-formed body stays in 4xx with no 5xx and no "
        "traceback (the view refuses the payload or the edge caps it)",
        f"POST /api/v1/store/cart/ (~{HUGE_PAD_BYTES} bytes)",
        f"{huge['status']} {snip(huge['body'], 120)}; "
        f"no traceback: {_no_traceback(huge['body'])}",
        verdict(huge_ok and _no_traceback(huge["body"])),
        "n/a" if huge_ok else "Medium",
    )


# --------------------------------------------------------------------------
# Family 8: oracles / info leaks
# --------------------------------------------------------------------------
def phase8(H: Http, F, bare: Http):
    ensure_session(bare)

    # -- F8-E1: product existence differential ------------------------------
    if SHARED.get("seeded"):
        resp = H.request("/api/v1/store/products/")
        results = json.loads(resp["body"]).get("results") or []
        slug = results[0]["slug"] if results else "zz-none"
        times = {"exists": [], "absent": []}
        statuses = {"exists": [], "absent": []}
        for _ in range(5):
            for key, path in (
                ("exists", f"/api/v1/store/products/{slug}/"),
                ("absent", "/api/v1/store/products/zz-t1b-absent/"),
            ):
                r = _timed(H, path)
                times[key].append(r["elapsed_ms"])
                statuses[key].append(r["status"])
        ok = all(s in (200, 404) for side in statuses.values() for s in side)
        F.add(
            "F8-E1",
            "8",
            f"product existence timing/status: 5x existing ({slug}) vs 5x "
            "absent detail GETs",
            "delisted/absent uniformity already pinned byte-identically "
            "(F5-D1); the timing medians must not separate existence "
            "beyond noise",
            "GET product detail x10",
            f"statuses exists={statuses['exists']} "
            f"absent={statuses['absent']}; "
            f"medians {_median(times['exists'])}ms vs "
            f"{_median(times['absent'])}ms (status 200/404 is the public "
            "catalogue's by-design difference, not an oracle)",
            verdict(ok),
        )
    else:
        _blocked(
            F,
            "F8-E1",
            "8",
            "product existence timing/status differential (existing vs "
            "absent detail GETs, medians over 5 shots each)",
            "an existing row must not be distinguishable from an absent "
            "one by timing beyond noise (status uniformity for delisted "
            "vs absent already proved in T1a F5-D1)",
            "the catalogue is empty (products count=0, seed_demo never "
            "ran) - there is no existing row to compare against; only the "
            "absent side exists",
        )

    # -- F8-E2: order id uniformity (anon) ---------------------------------
    o_times = {"id1": [], "id999": []}
    o_bodies = []
    for key, path in (
        ("id1", "/api/v1/store/orders/1/"),
        ("id999", "/api/v1/store/orders/999999/"),
    ):
        for _ in range(4):
            r = _timed(H, path)
            o_times[key].append(r["elapsed_ms"])
            if len(o_bodies) < 2 and r["status"] == 401:
                o_bodies.append(r)
    uniform = len(o_bodies) == 2 and same(o_bodies[0], o_bodies[1])
    F.add(
        "F8-E2",
        "8",
        "order existence oracle: anon GET order 1 vs 999999, 4 shots each "
        "(status, body, timing)",
        "order_detail is uniform-401 before any lookup - byte-identical "
        "bodies and no usable timing gap between a plausible and an "
        "absent id",
        "GET /api/v1/store/orders/{1,999999}/ x8",
        f"bodies byte-identical: {uniform}; medians "
        f"{_median(o_times['id1'])}ms vs {_median(o_times['id999'])}ms; "
        f"body {snip(o_bodies[0]['body'], 120) if o_bodies else '-'}",
        verdict(uniform),
    )

    # -- F8-E3: user existence via login (fresh auth window) ---------------
    _sleep_fresh("auth")
    fixture = SHARED.get("fixture_user", FIXTURE_USER)
    l_times = {"exists": [], "ghost": []}
    l_first = None
    g_first = None
    for i in range(4):
        e = _timed(
            H,
            "/api/v1/account/storefront/login/",
            method="POST",
            body={"username": fixture, "password": "wrong-password-x"},
        )
        l_times["exists"].append(e["elapsed_ms"])
        l_first = l_first or e
        g = _timed(
            H,
            "/api/v1/account/storefront/login/",
            method="POST",
            body={
                "username": f"ghost_{uuid.uuid4().hex[:8]}",
                "password": "wrong-password-x",
            },
        )
        l_times["ghost"].append(g["elapsed_ms"])
        g_first = g_first or g
    user_uniform = l_first is not None and same(l_first, g_first)
    F.add(
        "F8-E3",
        "8",
        "user existence oracle: login with a REAL registered username "
        "(inactive) vs a never-existing username, 4 shots each (status, "
        "body, timing)",
        "one uniform 401 for unknown, wrong-password and inactive - "
        "byte-identical bodies and no usable timing gap (no user-"
        "enumeration channel through login)",
        "POST storefront login x8 (fixture, ghosts)",
        f"statuses {l_first['status'] if l_first else '-'}/"
        f"{g_first['status'] if g_first else '-'}; byte-identical: "
        f"{user_uniform}; medians {_median(l_times['exists'])}ms vs "
        f"{_median(l_times['ghost'])}ms",
        verdict(user_uniform),
    )

    # -- F8-E4: cartless coupon oracle regression (T1a F3-C1 re-check) -----
    c1 = H.request(
        "/api/v1/store/orders/apply-coupon/",
        method="POST",
        body={"code": "T1B9A"},
    )
    c2 = H.request(
        "/api/v1/store/orders/apply-coupon/",
        method="POST",
        body={"code": "T1B9B"},
    )
    F.add(
        "F8-E4",
        "8",
        "cartless coupon existence differential (regression re-check of "
        "T1a F3-C1): two absent codes, no session",
        "the cartless preview answers one uniform not-found before it "
        "reads the code - no code-existence oracle",
        "POST /api/v1/store/orders/apply-coupon/ x2 (absent codes)",
        f"{c1['status']} vs {c2['status']}; byte-identical: {same(c1, c2)}",
        verdict(c1["status"] == 404 and same(c1, c2)),
    )

    # -- F8-H1: /health/ post-F2 split regression --------------------------
    health = H.request("/health/")
    try:
        payload = json.loads(health["body"])
    except ValueError:
        payload = {}
    checks = payload.get("checks") or {}
    leaked = [
        key
        for key in (
            "pending_orders",
            "carts",
            "low_stock",
            "out_of_stock",
            "razorpay_mode",
            "smtp_configured",
            "stock_alert_checked",
        )
        if key in payload or key in checks
    ]
    extra_checks = [
        key
        for key in checks
        if key not in ("database", "media_writable", "shipping_configured")
    ]
    F.add(
        "F8-H1",
        "8",
        "GET /health/ post-F2 split: scan the public body for the staff "
        "keys Q-S2 moved behind health_detail and for non-whitelisted "
        "checks",
        "the public probe carries only status + the deploy-relevant "
        "whitelist (database/media_writable/shipping_configured); no "
        "business counter or config mode reappears (Q-S2/F2 invariant, "
        "shape originally verified in T1a STEP ZERO)",
        "GET /health/",
        f"{health['status']}; checks={sorted(checks)}; staff keys leaked: "
        f"{leaked or 'none'}; extra checks: {extra_checks or 'none'}",
        verdict(
            health["status"] == 200
            and not leaked
            and not extra_checks
            and set(checks) <= {"database", "media_writable", "shipping_configured"},
        ),
    )

    # -- F8-H2: source maps on the storefront -------------------------------
    store = Http("https://test.boredompeaks.me", "storefront")
    index = _get_ref(store, "/")
    script_srcs = []
    if index["status"] == 200:
        script_srcs = re.findall(r'(?:src|href)="([^"]+\.js[^"]*)"', index["body"])[:3]
    map_hits = []
    map_statuses = []
    unresolved = []
    for src in script_srcs:
        # Probe PATHS, never absolute URLs: Http.request prefixes base, so
        # passing an absolute URL would build a malformed target and record
        # a transport error that looks like a probe (the bug this run
        # caught in its first f8 execution).
        if src.startswith("http"):
            parts = urlsplit(src)
            path = parts.path
        else:
            path = src
        probe = _get_ref(store, path + ".map")
        map_statuses.append(f"{path[:60]}.map={probe['status']}")
        if probe["status"] == 200:
            map_hits.append(path)
        if probe["status"] is None:
            unresolved.append(path)
    if index["status"] != 200 or not script_srcs:
        F.add(
            "F8-H2",
            "8",
            "source-map probe: fetch the storefront index and try "
            "<script>.map for each discovered bundle",
            "no published .map may serve the original sources",
            f"GET / on test.boredompeaks.me -> {index['status']}",
            f"index {index['status']}; bundles discovered: "
            f"{len(script_srcs)}; map probes: {map_statuses or 'none'}",
            "BLOCKED",
            "n/a",
        )
        SHARED["map_blocked"] = True
    elif map_hits:
        F.add(
            "F8-H2",
            "8",
            "source-map probe: fetch the storefront index, try "
            "<script>.map for up to 3 discovered bundles",
            "no published .map may serve the original sources (200 map = "
            "source disclosure)",
            f"GET / ({index['status']}) + {len(script_srcs)} .map GETs",
            f"bundles found: {len(script_srcs)}; "
            f"map probes: {map_statuses}; published maps: {map_hits}",
            "FAIL",
            "Medium",
        )
        SHARED["map_blocked"] = False
    elif unresolved:
        F.add(
            "F8-H2",
            "8",
            "source-map probe: fetch the storefront index, try "
            "<script>.map for up to 3 discovered bundles",
            "no published .map may serve the original sources",
            f"GET / ({index['status']}) + {len(script_srcs)} .map GETs",
            f"bundles found: {len(script_srcs)}; map probes: "
            f"{map_statuses}; transport-failed probes cannot prove "
            "absence",
            "BLOCKED",
            "n/a",
        )
        SHARED["map_blocked"] = True
    else:
        F.add(
            "F8-H2",
            "8",
            "source-map probe: fetch the storefront index, try "
            "<script>.map for up to 3 discovered bundles",
            "no published .map may serve the original sources (200 map = "
            "source disclosure)",
            f"GET / ({index['status']}) + {len(script_srcs)} .map GETs",
            f"bundles found: {len(script_srcs)}; "
            f"map probes: {map_statuses}; 200 maps: none",
            "PASS",
        )
        SHARED["map_blocked"] = False

    # -- F8-S1: stack traces under forced 5xx (reuse f7's bomb evidence) ----
    bomb = SHARED.get("bomb_nest") or {}
    huge = SHARED.get("bomb_huge") or {}
    # One more forced-error vector: a lone-surrogate JSON body the decoder
    # cannot round-trip.
    bad_bytes = b'{"pad": "\xed\xa0\x80"}'
    surrogate = bare.request(
        "/api/v1/store/cart/",
        method="POST",
        body=bad_bytes,
        content_type="application/json",
        headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
    )
    long_url = _timed(H, "/api/v1/store/products/?search=" + ("x" * 4000))
    vectors = [
        ("deep-nest-bomb", bomb),
        ("huge-json", huge),
        ("lone-surrogate", surrogate),
        ("long-url", long_url),
    ]
    five_xx = [
        (name, r)
        for name, r in vectors
        if isinstance(r.get("status"), int) and r["status"] >= 500
    ]
    leaked = [name for name, r in five_xx if not _no_traceback(r.get("body", ""))]
    statuses = [f"{name}={r.get('status')}" for name, r in vectors]
    F.add(
        "F8-S1",
        "8",
        "stack traces under forced errors: deep-nest bomb, huge JSON, "
        "lone-surrogate body, 4KB query string - every 5xx the run "
        "produced is scanned for traceback markers",
        "DEBUG stays off in production: any 5xx body must be the generic "
        "error page - no Traceback, no settings dump, no file paths",
        "4 forced-error vectors (2 from F7-B1/B2 evidence)",
        f"statuses: {', '.join(statuses)}; 5xx produced: "
        f"{[name for name, _ in five_xx] or 'none'}; traceback leaked: "
        f"{leaked or 'none'}",
        verdict(not leaked),
    )

    # -- F8-D1: Django debug page ------------------------------------------
    missing = H.request("/definitely-not-a-real-path-t1b/")
    admin_missing = H.request("/admin/definitely-not-a-real-path-t1b/")
    debug_markers = [
        name
        for name, r in (("api", missing), ("admin", admin_missing))
        if "DEBUG = True" in r["body"]
        or "You're seeing this error" in r["body"]
        or "Traceback (most recent call last)" in r["body"]
        or "Using the URLconf defined in" in r["body"]
    ]
    F.add(
        "F8-D1",
        "8",
        "Django debug page: two nonexistent paths (API root + admin) "
        "probed for the DEBUG traceback/settings pages",
        "DEBUG=False serves plain 404s (JSON envelope on API routes, "
        "Django's bare 404 elsewhere) - never the debug page",
        "GET /definitely-not-a-real-path-t1b/ + admin twin",
        f"statuses {missing['status']}/{admin_missing['status']}; debug "
        f"page markers: {'FOUND' if debug_markers else 'absent'}",
        verdict(
            missing["status"] == 404
            and not debug_markers
            and _no_traceback(missing["body"])
            and _no_traceback(admin_missing["body"]),
        ),
    )

    # -- F8-C1: CORS preflight abuse ----------------------------------------
    def preflight(origin):
        return H.request(
            "/api/v1/store/products/",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    evil = preflight("https://evil.example")
    control = preflight(TRUSTED_ORIGIN)
    acao_evil = evil["headers"].get("access-control-allow-origin", "")
    acao_ctrl = control["headers"].get("access-control-allow-origin", "")
    F.add(
        "F8-C1",
        "8",
        "CORS preflight abuse: evil origin (+ credential-shaped "
        "ACRH/ACRM) with a trusted-origin control",
        "the evil origin never receives Access-Control-Allow-Origin (a "
        "browser cannot read the response cross-origin); the control "
        "shows the probe measures CORS policy, not the absence of "
        "middleware",
        "OPTIONS /api/v1/store/products/ (evil, control)",
        f"evil status {evil['status']} ACAO={acao_evil or '<absent>'}; "
        f"control status {control['status']} ACAO="
        f"{acao_ctrl or '<absent>'}",
        verdict(acao_evil == ""),
    )

    # -- F8-C2: null origin + credentials tricks ---------------------------
    null_origin = preflight("null")
    simple = H.request(
        "/api/v1/store/products/",
        headers={"Origin": "https://evil.example"},
    )
    acao_null = null_origin["headers"].get("access-control-allow-origin", "")
    acao_simple = simple["headers"].get("access-control-allow-origin", "")
    allow_cred = simple["headers"].get("access-control-allow-credentials", "")
    F.add(
        "F8-C2",
        "8",
        "CORS null-origin + non-preflight evil-origin probes "
        "(credentials trick: allow-credentials without a matching ACAO)",
        "neither null nor evil ever receives ACAO; allow-credentials is "
        "never granted to an origin that got no ACAO",
        "OPTIONS (Origin: null) + GET (Origin: evil) on products",
        f"null ACAO={acao_null or '<absent>'} status "
        f"{null_origin['status']}; evil GET ACAO="
        f"{acao_simple or '<absent>'} status {simple['status']}; "
        f"allow-credentials={allow_cred or '<absent>'}",
        verdict(acao_null == "" and acao_simple == ""),
    )

    # -- F8-H3: Host-header / cache-poisoning probes ------------------------
    host_evil = H.request("/health/", headers={"Host": "evil.example"})
    xfh = H.request(
        "/api/v1/store/products",
        headers={"X-Forwarded-Host": "evil.example"},
    )
    host_redirect = H.request(
        "/api/v1/store/products", headers={"Host": "evil.example"}
    )
    reflections = []
    edge_rejected = []
    for name, resp in (
        ("health-host-evil", host_evil),
        ("slash-redirect-xfh", xfh),
        ("slash-redirect-host-evil", host_redirect),
    ):
        loc = resp["headers"].get("location", "")
        if loc and "evil.example" in loc:
            reflections.append(name + "-location")
        ctype = resp["headers"].get("content-type", "")
        body = resp["body"].lower()
        # An edge error page naming the Host it could not route is the edge
        # doing its job (Cloudflare pages carry its markers by design) - only
        # an APPLICATION body/header reflecting the attacker host counts.
        edge = "cloudflare" in body or "error 10" in body
        if not edge and "evil.example" in resp["body"] and ("json" in ctype):
            reflections.append(name + "-body")
        if edge:
            edge_rejected.append(f"{name}={resp['status']}(edge)")
    F.add(
        "F8-H3",
        "8",
        "Host-header/cache poisoning: Host: evil.example on /health/, "
        "X-Forwarded-Host on a slash-redirecting path, Host: evil on the "
        "same path (Location builders are the classic poison)",
        "the attacker host never appears in any body, header or Location "
        "- the edge/Django answer with the canonical host or refuse, "
        "never a reflected evil host a cache could key on",
        "GET x3 (Host/XFH overrides)",
        f"statuses {host_evil['status']}/{xfh['status']}/"
        f"{host_redirect['status']}; app reflection of evil.example: "
        f"{reflections or 'none'}; edge-rejected: "
        f"{edge_rejected or 'none'}; "
        f"xfh Location {snip(xfh['headers'].get('location', '-'), 100)}; "
        f"host Location "
        f"{snip(host_redirect['headers'].get('location', '-'), 100)}",
        verdict(not reflections),
    )


# --------------------------------------------------------------------------
# Family 9: notifications / outbox
# --------------------------------------------------------------------------
def phase9(H: Http, F, bare: Http):
    # -- F9-F1: recovery-trigger flood (resend-verification) ----------------
    # Empty email: throttle spends in initial() while the view's query
    # matches nobody - the budget is measured with zero outbound mail.
    _sleep_fresh("recovery")
    flood = []
    for _ in range(RECOVERY_SPRAY_CAP):
        flood.append(
            H.request(
                "/api/v1/account/resend-verification/",
                method="POST",
                body={},
            )
        )
        if flood[-1]["status"] == 429:
            break
    first_429 = _first_index(flood, 429)
    uniform = all(r["status"] == 200 for r in flood if r["status"] != 429)
    if first_429:
        result = (
            f"statuses=[200 x{first_429 - 1}, 429]; first refusal at shot "
            f"{first_429}/{len(flood)}; shipped default recovery=5/min; "
            f"no mail sent (empty email matches nobody); uniform: {uniform}"
        )
        ok = uniform
    else:
        result = (
            f"statuses=[200 x{len(flood)}]; NO 429 within cap "
            f"{RECOVERY_SPRAY_CAP} against shipped default recovery=5/min; "
            f"no mail sent; uniform: {uniform}"
        )
        ok = False
    F.add(
        "F9-F1",
        "9",
        f"enqueue/mail-flood via the resend-verification trigger: up to "
        f"{RECOVERY_SPRAY_CAP} shots in a fresh window (empty-email "
        "payloads spend the throttle without sending)",
        "the recovery scope refuses per-client within a bounded shot "
        "count, so the mail-trigger endpoint cannot be spammed; non-429 "
        "answers stay byte-uniform",
        f"POST /api/v1/account/resend-verification/ x{len(flood)}",
        result,
        verdict(ok),
        "n/a" if ok else "Medium",
    )

    # -- F9-F2: durable-outbox enqueue trigger reachability matrix ----------
    # Fresh SESSIONLESS identity on purpose: a sessioned POST without CSRF
    # material is refused by the CSRF layer before the view (T1a's
    # wrong-layer lesson), which would measure Django's referer check
    # instead of the trigger's own gate. No cookie = no CSRF question.
    matrix = Http(F.base, "matrix-sessionless")
    triggers = []
    webhook = matrix.request(
        "/api/v1/webhooks/razorpay/",
        method="POST",
        body={"event": "payment.captured", "payload": {"x": "y"}},
    )
    triggers.append(f"webhook={webhook['status']}")
    verify = matrix.request(
        "/api/v1/store/orders/payment/verify/",
        method="POST",
        body={
            "razorpay_order_id": "order_x",
            "razorpay_payment_id": "pay_x",
            "razorpay_signature": "a" * 64,
        },
    )
    triggers.append(f"verify={verify['status']}")
    checkout = matrix.request(
        "/api/v1/store/orders/checkout/",
        method="POST",
        body={"guest_email": "s@example.com"},
    )
    triggers.append(f"checkout={checkout['status']}")
    restock_path = "/api/v1/store/products/demo-plenty-perfume/restock-notifications/"
    restock = matrix.request(
        restock_path, method="POST", body={"email": "guest@redteam.invalid"}
    )
    triggers.append(f"restock={restock['status']}")
    # Every durable-outbox enqueue path (orders lifecycle, verify, gateway
    # hook) is gated before it can write a row; the restock arm needs a
    # product row (seed absent). The only reachable triggers are the INLINE
    # SMTP recovery views, whose own throttle is F9-F1.
    gated = (
        webhook["status"] == 503
        and verify["status"] == 401
        and checkout["status"] == 403
        and restock["status"] in (404, 429)
    )
    F.add(
        "F9-F2",
        "9",
        "reachable enqueue triggers for the durable outbox: forged "
        "webhook, anonymous payment verify, guest checkout, restock arm "
        "on the seeded slug (matrix of statuses)",
        "no unauthenticated caller can enqueue into NotificationOutbox: "
        "the webhook fails closed, verify is auth-gated, checkout is the "
        "guest deadend, restock arms only for a real product row (and is "
        "throttled on top)",
        "POST x4 (webhook, verify, checkout, restock)",
        "; ".join(triggers),
        verdict(gated),
    )

    # -- F9-F3: dedup-key collision -----------------------------------------
    _blocked(
        F,
        "F9-F3",
        "9",
        "dedup-key collision: enqueue the same event twice and observe "
        "the unique dedup_key collapse (or a collision across different "
        "payloads)",
        "the outbox's unique dedup_key refuses the second identical "
        "enqueue while occurrence-discriminated repeats still land "
        "(common/notifications.py _dedup_key)",
        "no API surface exposes outbox rows or enqueue outcomes: the "
        "dedup key is server-side only (enqueue happens inside order/"
        "payment transactions), and there is no staff or public read "
        "route for NotificationOutbox - the collision cannot be observed "
        "from outside the box",
    )

    # -- F9-F4: dead-letter alert abuse -------------------------------------
    forged = H.request(
        "/api/v1/account/password-reset/confirm/",
        method="POST",
        body={
            "uid": "not-a-real-uid",
            "token": "not-a-real-token",
            "password": "another harbor lane",
        },
    )
    refused = forged["status"] == 400 and _no_traceback(forged["body"])
    F.add(
        "F9-F4",
        "9",
        "dead-letter/security-alert abuse: forged password-reset "
        "confirmation (the one alert-firing branch an anonymous caller "
        "can even aim at) + normal /health/ (the integration-alert "
        "branch)",
        "the forged confirm dies at the token gate (uniform 400, no "
        "traceback) before the branch that records AUTH_PASSWORD_RESET "
        "and fires notify_security_change; the health-alert branch only "
        "fires on a degraded probe, which a caller cannot induce",
        "POST /api/v1/account/password-reset/confirm/ (forged uid/token)",
        f"{forged['status']} {snip(forged['body'], 130)}; refused at "
        f"token gate: {refused}",
        verdict(refused),
    )

    # -- F9-F5: drain worker under poisoned payloads ------------------------
    _blocked(
        F,
        "F9-F5",
        "9",
        "drain worker behaviour under poisoned payload rows (corrupt "
        "context references, oversized error strings, missing models)",
        "drain_notifications claims, bounded-retries and dead-letters "
        "poisoned rows without crashing the worker (the property the "
        "backend suite pins in-process)",
        "the drain runs server-side on the celery beat schedule - no API "
        "route enqueues a poisoned context or reads a drain outcome, so "
        "the attack has no externally observable leg on this deployment "
        "(box-side verification belongs to T2/ops with shell access)",
    )

    # -- F9-F6: restock arm flood -------------------------------------------
    if SHARED.get("seeded"):
        _sleep_fresh("restock")
        shots = []
        for _ in range(RESTOCK_SPRAY_CAP):
            shots.append(
                bare.request(
                    restock_path,
                    method="POST",
                    body={"email": "guest@redteam.invalid"},
                    headers=browser_csrf(bare.jar, TRUSTED_ORIGIN),
                )
            )
            if shots[-1]["status"] == 429:
                break
        first_429 = _first_index(shots, 429)
        statuses = _status_list(shots)
        F.add(
            "F9-F6",
            "9",
            f"restock-arm flood: up to {RESTOCK_SPRAY_CAP} guest "
            "notifications on the seeded slug, same (IP, email) pair",
            "the dual restock/restock-guest throttle refuses the pair "
            "within a bounded shot count",
            f"POST {restock_path} x{len(shots)}",
            f"statuses={statuses}; first refusal at shot {first_429 or '-'}",
            verdict(first_429 is not None),
            "n/a" if first_429 else "Medium",
        )
    else:
        _blocked(
            F,
            "F9-F6",
            "9",
            "restock-arm enqueue flood on a real product (12-shot burst, "
            "same guest email)",
            "the restock pair throttle bounds a flood that actually arms "
            "notification rows",
            "no product row exists to arm against (seed_demo never ran; "
            "the seeded-slug probe404s) - only the not-found path was "
            "reachable, see F9-F2's matrix",
        )


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument(
        "--out",
        default=None,
        help="evidence JSON path (default: beside this script, so the run "
        "works from any working directory)",
    )
    ap.add_argument(
        "--only",
        default="env,f7,f6,f8,f9",
        help="comma list of phases: env,f6,f7,f8,f9 "
        "(default order puts f7 before f6 so the register-scope flood in "
        "f6 starts from a clean window)",
    )
    args = ap.parse_args()
    phases = [p.strip() for p in args.only.split(",") if p.strip()]
    out_path = args.out or str(pathlib.Path(__file__).with_name("t1b_evidence.json"))

    F = t1a.Ledger(args.base)
    H = Http(args.base, "identity")
    cart_ref = Http(args.base, "cartless-ref")
    fam_by_phase = {"f6": "6", "f7": "7", "f8": "8", "f9": "9"}
    refreshed_fams = {fam_by_phase[p] for p in phases if p in fam_by_phase}

    # Carry env verdicts forward: a later `--only f8` reuses the previous
    # run's STEP ZERO verdicts instead of silently branching wrong.
    try:
        with open(out_path, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("meta", {}).get("base") == args.base:
            SHARED.update(old.get("meta", {}).get("env_state", {}))
    except (OSError, ValueError):
        pass

    def dump():
        rows, env_rows = F.rows, F.env
        try:
            with open(out_path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = None
        if old and old.get("meta", {}).get("base") == args.base:
            if "env" not in phases:
                env_rows = old.get("env", [])
            rows = [
                r for r in old.get("rows", []) if r["family"] not in refreshed_fams
            ] + F.rows
        doc = {
            "meta": {
                "base": args.base,
                "task": "SPEC-T1b families 6-9",
                "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                "env_state": SHARED,
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
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=True)

    runner = {
        "env": lambda: phase_env(H, F, args.base),
        "f6": lambda: phase6(H, F, cart_ref),
        "f7": lambda: phase7(H, F, cart_ref),
        "f8": lambda: phase8(H, F, cart_ref),
        "f9": lambda: phase9(H, F, cart_ref),
    }
    try:
        for name in phases:
            runner[name.strip()]()
            dump()
            c = F.counts()
            print(
                f"[{name}] rows={c['total']} pass={c['pass']} "
                f"fail={c['fail']} blocked={c['blocked']}"
            )
    except Exception as e:  # noqa: BLE001 - keep whatever rows exist
        dump()
        print(f"RUN BROKE at {phases}: {e!r}; partial evidence written")
        return 1
    c = F.counts()
    print(
        f"DONE total={c['total']} pass={c['pass']} fail={c['fail']} "
        f"blocked={c['blocked']} -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
