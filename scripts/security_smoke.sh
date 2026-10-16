#!/usr/bin/env bash
# SPEC-T1a / T1 deliverable: security regression smoke, families 1-5.
#
# Fast anonymous-only subset of the red-team catalog (T1-report.md), meant to
# re-run after every future release: each check asserts an invariant that
# survived the live attack run, so a regression turns this script red before
# anyone re-attacks. T1b appends the families 6-10 subset later.
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

same_body() { # desc other_file
  if cmp -s "$LAST_BODY" "$2"; then pass "$1 (byte-identical)"; else
    fail "$1: bodies differ ($(head -c 120 "$LAST_BODY") vs $(head -c 120 "$2"))"
  fi
}

echo "== security smoke (families 1-5) against $BASE"

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

echo "== $CHECKS checks, $FAILS failed"
if [ "$FAILS" -gt 0 ]; then exit 1; fi
exit 0
