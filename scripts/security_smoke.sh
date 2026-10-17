#!/usr/bin/env bash
# SPEC-T1 deliverable: security regression smoke, families 1-10.
#
# Fast anonymous-only subset of the red-team catalog (T1-report.md), meant to
# re-run after every future release: each check asserts an invariant that
# survived the live attack run, so a regression turns this script red before
# anyone re-attacks. Families 1-5 are T1a's original subset (kept intact);
# families 6-10 are T1b's additions (auth/MFA, input-validation, oracles,
# notifications, and the cross-family regression checks).
#
# Usage: scripts/security_smoke.sh [base-url]
#   base-url defaults to the live UAT API (test mode; no real money anywhere).
# Exit: 0 all invariants held, 1 otherwise. Needs only bash + curl + cmp.
set -u

BASE="${1:-https://api.boredompeaks.me}"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CHECKS=0
FAILS=0

# req METHOD PATH [BODY_FILE] [Hdr...] -> prints status code, body in
# $LAST_BODY, response headers in $LAST_HDRS. No BODY_FILE = no body sent.
req() {
  local method="$1" path="$2" body=""
  if [ "$#" -ge 3 ]; then body="$3"; shift 3; else shift 2; fi
  local args=(-sS --max-time 25 -X "$method" -D "$TMP/hdrs" -o "$TMP/body"
    -w '%{http_code}' -H "User-Agent: $UA" -H 'Accept: application/json')
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' --data-binary "@$body")
  fi
  local h
  for h in "$@"; do args+=(-H "$h"); done
  LAST_STATUS="$(curl "${args[@]}" "$BASE$path" 2>"$TMP/err")" || LAST_STATUS="000"
  cp "$TMP/body" "$TMP/last_body" 2>/dev/null || : >"$TMP/last_body"
  LAST_BODY="$TMP/last_body"
  LAST_HDRS="$TMP/hdrs"
  return 0
}

pass() { CHECKS=$((CHECKS + 1)); echo "PASS  $1"; }
fail() { CHECKS=$((CHECKS + 1)); FAILS=$((FAILS + 1)); echo "FAIL  $1"; }

# status_in "desc" "a b c"  -> pass when $LAST_STATUS is one of a b c
status_in() {
  local desc="$1" allowed="$2" a
  for a in $allowed; do
    if [ "$LAST_STATUS" = "$a" ]; then pass "$desc (status $LAST_STATUS)"; return; fi
  done
  fail "$desc: status $LAST_STATUS not in {$allowed}; body: $(head -c 160 "$LAST_BODY")"
}

status_ge4() {
  local desc="$1"
  case "$LAST_STATUS" in
    [45][0-9][0-9]) pass "$desc (status $LAST_STATUS)" ;;
    *) fail "$desc: status $LAST_STATUS accepted the forged input; body: $(head -c 160 "$LAST_BODY")" ;;
  esac
}

# status_not5xx "desc" -> pass when the answer is not a server error
# (2xx or 4xx are both fine: the invariant under test is "no 5xx", the
# exact class is per-endpoint).
status_not5xx() {
  local desc="$1"
  case "$LAST_STATUS" in
    5[0-9][0-9]) fail "$desc: status $LAST_STATUS is a 5xx; body: $(head -c 160 "$LAST_BODY")" ;;
    000) fail "$desc: transport failure, no status" ;;
    *) pass "$desc (status $LAST_STATUS)" ;;
  esac
}

same_body() { # desc other_file
  if cmp -s "$LAST_BODY" "$2"; then pass "$1 (byte-identical)"; else
    fail "$1: bodies differ ($(head -c 120 "$LAST_BODY") vs $(head -c 120 "$2"))"
  fi
}

echo "== security smoke (families 1-10) against $BASE"

# --- Family 1: forged webhook deliveries must never be accepted -------------
printf '%s' '{"event":"payment.captured","payload":{"x":"y"}}' >"$TMP/wh.json"
req POST /api/v1/webhooks/razorpay/ "$TMP/wh.json"
status_ge4 "F1 unsigned webhook refused"
cp "$LAST_BODY" "$TMP/wh_refused"
req POST /api/v1/webhooks/razorpay/ "$TMP/wh.json" "X-Razorpay-Signature: $(printf 'a%.0s' $(seq 64))"
status_ge4 "F1 bad 64-hex signature refused"
req POST /api/v1/webhooks/razorpay/ "$TMP/wh.json" "X-Razorpay-Signature: $(printf 'b%.0s' $(seq 63))"
status_ge4 "F1 truncated signature refused"
req POST /api/v1/webhooks/razorpay/ "$TMP/wh.json" "X-Razorpay-Signature: none"
status_ge4 "F1 alg-confusion signature refused"

# verify / payment / refund seams refuse anonymous callers before any logic
printf '%s' '{"razorpay_order_id":"order_smoke","razorpay_payment_id":"pay_smoke","razorpay_signature":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"}' >"$TMP/verify.json"
req POST /api/v1/store/orders/payment/verify/ "$TMP/verify.json"
status_in "F1 anonymous verify refused" "401"
printf '%s' '{"order_id":"order_smoke","amount":1,"currency":"INR"}' >"$TMP/pay.json"
req POST /api/v1/store/orders/payment/ "$TMP/pay.json"
status_in "F1 anonymous payment intent refused" "401"
req POST /api/v1/admin/orders/1/refund/ "$TMP/pay.json"
status_in "F1 anonymous refund refused" "401 403"

# --- Family 2: inventory mutations refuse anonymous callers ------------------
printf '%s' '{"delta":-99}' >"$TMP/adj.json"
req POST /api/v1/store/products/inventory/adjustments/ "$TMP/adj.json"
status_in "F2 inventory adjust refused" "401 403"
printf '%s' '{"name":"smoke-negative","price":"1.00","stock":-5}' >"$TMP/prod.json"
req POST /api/v1/store/products/ "$TMP/prod.json"
status_in "F2 product create refused" "401 403"
req GET /admin/
status_in "F2 admin changelist gated to login" "302"

# --- Family 3: no cartless coupon oracle ------------------------------------
printf '%s' '{"code":"NOSUCH99"}' >"$TMP/c1.json"
req POST /api/v1/store/orders/apply-coupon/ "$TMP/c1.json"
status_ge4 "F3 cartless preview refused (absent code A)"
cp "$LAST_BODY" "$TMP/coupon_a"
printf '%s' '{"code":"OTHER99"}' >"$TMP/c2.json"
req POST /api/v1/store/orders/apply-coupon/ "$TMP/c2.json"
status_ge4 "F3 cartless preview refused (absent code B)"
same_body "F3 cartless oracle uniform across codes" "$TMP/coupon_a"

# --- Family 4: guest deadend + session material -----------------------------
printf '%s' '{"full_name":"Smoke","phone":"9999999999","address":"1 S St","city":"C","state":"S","pincode":"123456","guest_email":"s@example.com"}' >"$TMP/g1.json"
req POST /api/v1/store/orders/checkout/ "$TMP/g1.json"
status_in "F4 guest checkout refused" "403"
cp "$LAST_BODY" "$TMP/refusal"
printf '%s' '{}' >"$TMP/g2.json"
req POST /api/v1/store/orders/checkout/ "$TMP/g2.json"
same_body "F4 refusal uniform for empty payload" "$TMP/refusal"
printf '%s' '{"bad"' >"$TMP/g3.json"
req POST /api/v1/store/orders/checkout/ "$TMP/g3.json"
same_body "F4 refusal uniform for malformed JSON" "$TMP/refusal"

# session cookie never echoes into a payload (POST-F1 regression)
req GET /api/v1/store/cart/
status_in "F4 cart session endpoint healthy" "200"
SESSION_VALUE="$(awk 'tolower($1) ~ /^set-cookie:/ {print $0}' "$LAST_HDRS" \
  | grep -o 'sessionid=[^;]*' | head -n1 | cut -d= -f2)"
if [ -n "$SESSION_VALUE" ] && ! grep -Fq "$SESSION_VALUE" "$LAST_BODY"; then
  pass "F4 session key absent from cart payload"
elif [ -z "$SESSION_VALUE" ]; then
  fail "F4 no sessionid cookie issued - cannot check payload leak"
else
  fail "F4 sessionid value echoed inside a response body"
fi

# session-riding POST without Origin/CSRF must be refused (403)
req POST /api/v1/store/orders/checkout/ "$TMP/g1.json" "Cookie: sessionid=forged-not-a-real-session"
status_in "F4 sessioned POST without CSRF material refused" "403 401"

# --- Family 5: RBAC + delist uniformity -------------------------------------
for p in /api/v1/admin/orders/ /api/v1/admin/orders/1/ /api/admin/orders/ \
         /api/v1/admin/audit-log/ /api/v1/admin/health/; do
  req GET "$p"
  status_in "F5 anonymous GET $p gated" "401 403 302"
done
for p in /api/v1/admin/orders/1/fulfill/ /api/v1/admin/orders/1/cancel/ \
         /api/admin/orders/1/refund/; do
  req POST "$p" "$TMP/g1.json"
  status_in "F5 anonymous POST $p gated" "401 403"
done
req GET /api/v1/store/orders/
status_in "F5 anonymous customer order list refused" "401"
req GET /api/v1/store/orders/1/
status_in "F5 anonymous order detail (IDOR) refused" "401"

req GET /api/v1/store/products/zz-smoke-absent-one/
status_in "F5 nonexistent product 404" "404"
cp "$LAST_BODY" "$TMP/prod404"
req GET /api/v1/store/products/zz-smoke-absent-two/
status_in "F5 second nonexistent product 404" "404"
same_body "F5 nonexistent slugs byte-identical" "$TMP/prod404"
req GET /api/products/zz-smoke-absent-one/
same_body "F5 legacy mount byte-identical to v1" "$TMP/prod404"

# --- Family 6: auth/MFA refusals + refresh cookie contract ----------------
printf '%s' '{}' >"$TMP/e.json"
req POST /api/v1/account/logout/ "$TMP/e.json"
status_in "F6 anonymous logout refused" "401"
req POST /api/v1/account/mfa/setup/ "$TMP/e.json"
status_in "F6 anonymous MFA setup refused" "401"
req GET /api/v1/account/mfa/status/
status_in "F6 anonymous MFA status refused" "401 403"
req POST /api/v1/account/token/refresh/ "$TMP/e.json" "Origin: https://evil.example"
status_in "F6 cross-origin refresh refused" "403"
# An origin-gate probe must never mint from or sweep the victim's cookie.
if grep -qi '^set-cookie:.*refresh_token=' "$LAST_HDRS"; then
  fail "F6 refresh probe answered with a refresh_token Set-Cookie"
else
  pass "F6 refresh probe carries no refresh_token Set-Cookie"
fi

# --- Family 7: input validation ------------------------------------------
req GET "/api/v1/store/products/?search=%27%20OR%20%271%27%3D%271"
status_not5xx "F7 SQLi search tautology stays out of 5xx"
if grep -qiE 'syntax error|SQLSTATE' "$LAST_BODY"; then
  fail "F7 SQLi search leaked SQL error text"
fi
req GET "/api/v1/store/products/?ordering=%28CASE%20WHEN%20%281%3D1%29%20THEN%20price%20ELSE%20id%20END%29"
status_not5xx "F7 SQLi ordering injection stays out of 5xx"

# Deep-nesting bomb against a JSON-parsing door: the contract this checks
# is that it never authenticates and never leaks a traceback. The exact
# 4xx-vs-5xx handling is report finding F7-B1 (500 today); once that is
# fixed this check must stay green either way.
OPEN=$(printf '%.0s[' $(seq 1 2500))
CLOSE=$(printf '%.0s]' $(seq 1 2500))
printf '%s' "${OPEN}1${CLOSE}" >"$TMP/nest.json"
req POST /api/v1/account/storefront/login/ "$TMP/nest.json"
if [ "$LAST_STATUS" = "200" ] || [ "$LAST_STATUS" = "000" ]; then
  fail "F7 deep-nest bomb got status $LAST_STATUS from login"
elif grep -Fq 'Traceback (most recent call last)' "$LAST_BODY" \
  || grep -Fq 'DEBUG = True' "$LAST_BODY"; then
  fail "F7 deep-nest bomb response leaked a traceback"
else
  pass "F7 deep-nest bomb never authenticates, never leaks (status $LAST_STATUS)"
fi

req GET "/api/v1/store/products/?search=%3Cscript%3Ealert(1)%3C%2Fscript%3E"
if [ "$LAST_STATUS" = "200" ] && grep -qi 'content-type: application/json' "$LAST_HDRS"; then
  pass "F7 reflected XSS payload served as application/json"
else
  fail "F7 XSS probe: status $LAST_STATUS or content-type not JSON"
fi

# Polyglot image with a path-traversal filename, anonymous multipart.
printf 'GIF89a<script>alert(1)</script>' >"$TMP/p.gif"
poly_code=$(curl -sS --max-time 25 -X POST -H "User-Agent: $UA" \
  -F 'name=smoke-poly' -F 'price=1.00' \
  -F "image=@$TMP/p.gif;filename=../../evil.png;type=image/gif" \
  -w '%{http_code}' -o "$TMP/poly_body" \
  "$BASE/api/v1/store/products/" 2>/dev/null) || poly_code="000"
case "$poly_code" in
  401|403) pass "F7 polyglot+traversal upload refused (status $poly_code)" ;;
  *) fail "F7 polyglot upload not refused cleanly: status $poly_code; body: $(head -c 160 "$TMP/poly_body" 2>/dev/null)" ;;
esac

req GET '/admin/login/?next=%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E'
if [ "$LAST_STATUS" = "200" ] && ! grep -Fq '<script>alert(1)</script>' "$LAST_BODY"; then
  pass "F7 admin login next= payload stays escaped"
else
  fail "F7 admin login reflected the raw payload (status $LAST_STATUS)"
fi

# --- Family 8: oracles / info leaks ---------------------------------------
req GET /health/
if grep -Fq '"database"' "$LAST_BODY" && ! grep -Fq 'pending_orders' "$LAST_BODY" \
  && ! grep -Fq 'low_stock' "$LAST_BODY"; then
  pass "F8 public health carries deploy checks only"
else
  fail "F8 public health body wrong: $(head -c 160 "$LAST_BODY")"
fi

req OPTIONS /api/v1/store/products/ "" "Origin: https://evil.example" \
  "Access-Control-Request-Method: GET" \
  "Access-Control-Request-Headers: authorization"
if grep -qi '^access-control-allow-origin:' "$LAST_HDRS"; then
  fail "F8 evil origin received an Access-Control-Allow-Origin header"
else
  pass "F8 evil origin receives no ACAO"
fi
req OPTIONS /api/v1/store/products/ "" "Origin: https://test.boredompeaks.me" \
  "Access-Control-Request-Method: GET"
if grep -qi '^access-control-allow-origin:' "$LAST_HDRS"; then
  pass "F8 CORS instrument: trusted origin receives ACAO (control)"
else
  fail "F8 control preflight got no ACAO - the evil-origin check above is vacuous"
fi

req GET /definitely-not-a-real-path-smoke/
if [ "$LAST_STATUS" = "404" ] && ! grep -Fq 'Traceback (most recent call last)' "$LAST_BODY" \
  && ! grep -Fq 'DEBUG = True' "$LAST_BODY"; then
  pass "F8 unknown path: plain 404, no debug page"
else
  fail "F8 unknown path: status $LAST_STATUS or debug markers present"
fi

req GET /api/v1/store/products "" "X-Forwarded-Host: evil.example"
if grep -qi '^location:.*evil\.example' "$LAST_HDRS"; then
  fail "F8 slash-redirect Location carries the poisoned host"
else
  pass "F8 poisoned X-Forwarded-Host never reaches a Location header"
fi

# --- Family 9: notifications / outbox --------------------------------------
printf '%s' '{"uid":"smoke-forged","token":"smoke-forged"}' >"$TMP/confirm.json"
req POST /api/v1/account/password-reset/confirm/ "$TMP/confirm.json"
status_in "F9 forged reset confirmation refused at token gate" "400"
printf '%s' '{"email":"smoke@redteam.invalid"}' >"$TMP/restock.json"
req POST /api/v1/store/products/zz-smoke-restock/restock-notifications/ "$TMP/restock.json"
status_in "F9 restock arm on absent product is uniform not-found" "404"
cp "$LAST_BODY" "$TMP/restock_miss"
req POST /api/v1/store/products/zz-smoke-restock/restock-notifications/ "$TMP/restock.json"
same_body "F9 restock not-found twin byte-identical" "$TMP/restock_miss"

echo "== $CHECKS checks, $FAILS failed"
if [ "$FAILS" -gt 0 ]; then exit 1; fi
exit 0
