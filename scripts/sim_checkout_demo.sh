#!/usr/bin/env bash
# Takes a brand-new (Free) user to Pro through the SIMULATED checkout, by HTTP only:
#   /me (free) -> POST /checkout -> the simulated page's pay call -> POST /checkout/verify
#   -> /me (Pro) -> /invoices (an SI/... invoice).
# Needs curl, and billing running with PAYMENT_SIMULATION=true (see runbooks/simulation.md).
# Works in Git Bash on Windows and on Linux/macOS. No jq or python needed.
#
# Direct to billing (default):   BILLING_URL=http://localhost:8080 GATEWAY_SECRET=... ./sim_checkout_demo.sh
# Through api-gateway:           BASE_URL=http://localhost:8000 USER_JWT=<jwt> ./sim_checkout_demo.sh
set -euo pipefail

BILLING_URL="${BILLING_URL:-http://localhost:8080}"
GATEWAY_SECRET="${GATEWAY_SECRET:-dev-gateway-secret}"
PLAN="${PLAN:-pro}"
INTERVAL="${INTERVAL:-month}"
STATE_CODE="${STATE_CODE:-10}"
EMAIL="${EMAIL:-demo@example.com}"
CARD="${CARD:-4111111111111111}"
OTP="${OTP:-123456}"
WAIT_SECONDS="${WAIT_SECONDS:-30}"

if [ -n "${USER_JWT:-}" ]; then # through the gateway: it sets the identity headers from the JWT
  BASE="${BASE_URL:-http://localhost:8000}/api/v1/billing"
  AUTH=(-H "Authorization: Bearer ${USER_JWT}")
  PUBLIC_BASE="${BASE}"
else # straight to billing: this script plays the gateway
  USER_ID="${USER_ID:-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n' |
    sed -E 's/^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*$/\1-\2-4\3-a\4-\5/')}"
  BASE="${BILLING_URL}/billing/v1"
  AUTH=(-H "X-Gateway-Secret: ${GATEWAY_SECRET}" -H "X-User-Id: ${USER_ID}")
  PUBLIC_BASE="${BASE}"
  echo "user: ${USER_ID}"
fi

fail() { echo "FAILED: $*" >&2; exit 1; }
uuid() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n' |
  sed -E 's/^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*$/\1-\2-4\3-a\4-\5/'; }
# api <method> <path> [json body] -> prints the body, fails on a non-2xx
api() {
  local method="$1" path="$2" body="${3:-}" out status
  out="$(mktemp)"
  if [ "$method" = GET ]; then
    status="$(curl -sS -o "$out" -w '%{http_code}' "${AUTH[@]}" "${BASE}${path}")"
  else
    status="$(curl -sS -o "$out" -w '%{http_code}' -X "$method" "${AUTH[@]}" \
      -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuid)" -d "$body" "${BASE}${path}")"
  fi
  case "$status" in 2??) cat "$out"; rm -f "$out" ;; *) cat "$out" >&2; rm -f "$out"; fail "$method $path -> HTTP $status" ;; esac
}
# field <name> <json>: the first string value of "name" (compact JSON, as billing sends it)
field() { printf '%s' "$2" | grep -o "\"$1\":\"[^\"]*\"" | head -n1 | sed -E "s/^\"$1\":\"(.*)\"$/\1/"; }

echo "1. a new user is on the Free plan"
ME="$(api GET /me)"
[ "$(field code "$ME")" = free ] || fail "expected the Free plan, got: $ME"
echo "   payment_mode: $(field payment_mode "$ME")"
[ "$(field payment_mode "$ME")" = simulated ] || fail "billing is not running with PAYMENT_SIMULATION=true"

echo "2. start the checkout"
CHECKOUT="$(api POST /checkout "{\"plan_code\":\"${PLAN}\",\"interval\":\"${INTERVAL}\",\"state_code\":\"${STATE_CODE}\",\"email\":\"${EMAIL}\"}")"
[ "$(field provider "$CHECKOUT")" = simulator ] || fail "expected provider=simulator: $CHECKOUT"
SIM_URL="$(field simulator_url "$CHECKOUT")"
[ -n "$SIM_URL" ] || fail "no simulator_url in: $CHECKOUT"
echo "   simulator_url: ${SIM_URL}"

# the page lives at /billing/v1/sim/checkout/<id>?t=<token> on billing (/api/v1/billing/... on the gateway)
PATH_PART="${SIM_URL#/api/v1/billing}"
PAGE_URL="${PUBLIC_BASE}${PATH_PART}"
echo "3. open the simulated page (what the browser popup does)"
curl -sS "${PAGE_URL}" | grep -q 'SIMULATED PAYMENT' || fail "the simulated checkout page did not load"

echo "4. pay with the test card ${CARD} and OTP ${OTP}"
PAY_URL="${PUBLIC_BASE}${PATH_PART%%\?*}/pay?${PATH_PART#*\?}"
HANDLER="$(curl -sS --fail-with-body -X POST -H 'Content-Type: application/json' \
  -d "{\"method\":\"card\",\"card_number\":\"${CARD}\",\"otp\":\"${OTP}\"}" "${PAY_URL}")" ||
  fail "payment failed: ${HANDLER}"
PAYMENT_ID="$(field razorpay_payment_id "$HANDLER")"
SUBSCRIPTION_ID="$(field razorpay_subscription_id "$HANDLER")"
SIGNATURE="$(field razorpay_signature "$HANDLER")"
[ -n "$PAYMENT_ID" ] && [ -n "$SIGNATURE" ] || fail "no Razorpay handler fields in: $HANDLER"

echo "5. verify, like the frontend does with the popup's postMessage"
VERIFIED="$(api POST /checkout/verify "{\"razorpay_payment_id\":\"${PAYMENT_ID}\",\"razorpay_subscription_id\":\"${SUBSCRIPTION_ID}\",\"razorpay_signature\":\"${SIGNATURE}\"}")"
echo "   ${VERIFIED}"

echo "6. wait for Pro (webhooks are delivered by the worker; up to ${WAIT_SECONDS}s)"
for _ in $(seq 1 "$WAIT_SECONDS"); do
  ME="$(api GET /me)"
  [ "$(field code "$ME")" = "$PLAN" ] && break
  sleep 1
done
[ "$(field code "$ME")" = "$PLAN" ] || fail "the user never reached ${PLAN}: $ME"
echo "   plan: $(field code "$ME"), status: $(field status "$ME")"

echo "7. the invoice comes from the simulated series (SI/...)"
NUMBER=""
for _ in $(seq 1 "$WAIT_SECONDS"); do
  INVOICES="$(api GET /invoices)"
  NUMBER="$(field number "$INVOICES")"
  [ -n "$NUMBER" ] && break
  sleep 1
done
case "$NUMBER" in SI/*) echo "   invoice: ${NUMBER}" ;; *) fail "expected an SI/ invoice, got '${NUMBER}'" ;; esac

echo "OK: Free -> ${PLAN} with invoice ${NUMBER} (simulated payment, no real money)"
