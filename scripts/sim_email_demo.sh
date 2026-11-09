#!/usr/bin/env bash
# The billing emails, end to end in simulation: the P1 checkout demo (a receipt with the SI invoice
# PDF), then a failed renewal forced through the simulator control API (payment_failed), then the
# grace period running out (subscription_suspended). Lists the .eml files it produced.
#
# The WORKER sends the mail, so start billing's worker with these settings (see runbooks/email.md):
#   PAYMENT_SIMULATION=true EMAIL_MODE=file EMAIL_OUTBOX_DIR=./.email-outbox
#   GRACE_PERIOD_SECONDS=60 SIM_FAST_JOBS=true SIM_WEBHOOK_DELAY_SECONDS=1
# and this script must see the same directory (EMAIL_OUTBOX_DIR, relative to where you run it).
#
# Optional: PSQL="docker exec -i <postgres container> psql -U billing_migrator -d billing_db -tA"
# turns on the last check: the simulator's webhooks are delivered a second time and no email is
# duplicated. Works in Git Bash on Windows and on Linux/macOS.
set -euo pipefail

BILLING_URL="${BILLING_URL:-http://localhost:8080}"
GATEWAY_SECRET="${GATEWAY_SECRET:-dev-gateway-secret}"
OUTBOX="${EMAIL_OUTBOX_DIR:-./.email-outbox}"
GRACE_WAIT="${GRACE_WAIT:-180}"
HERE="$(cd "$(dirname "$0")" && pwd)"

fail() { echo "FAILED: $*" >&2; exit 1; }
uuid() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n' |
  sed -E 's/^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*$/\1-\2-4\3-a\4-\5/'; }

USER_ID="${USER_ID:-$(uuid)}"
EMAIL="demo-${USER_ID%%-*}@example.com" # unique per run, so old files in the outbox do not count
export USER_ID EMAIL BILLING_URL GATEWAY_SECRET
mkdir -p "$OUTBOX"

# eml_count <template>: files in the outbox addressed to this run's user, made from that template
eml_count() {
  local n=0 f
  for f in "$OUTBOX"/*.eml; do
    [ -e "$f" ] || continue
    grep -q "^To: .*${EMAIL}" "$f" && grep -q "^X-GranthanAI-Template: $1" "$f" && n=$((n + 1))
  done
  echo "$n"
}
wait_for() { # wait_for <template> <seconds>
  local i
  for i in $(seq 1 "$2"); do
    [ "$(eml_count "$1")" -ge 1 ] && return 0
    sleep 1
  done
  return 1
}

echo "== 1. checkout demo (user ${USER_ID}, ${EMAIL})"
bash "$HERE/sim_checkout_demo.sh" | tail -4
wait_for receipt 60 || fail "no receipt email arrived in ${OUTBOX} (is the worker running with EMAIL_MODE=file?)"
echo "   receipt email written"

echo "== 2. force a failed renewal with the simulator control API"
STAFF=(-H "X-Gateway-Secret: ${GATEWAY_SECRET}" -H "X-Staff-Role: billing_admin")
STATE="$(curl -sS "${STAFF[@]}" "${BILLING_URL}/billing/v1/sim/state?user_id=${USER_ID}")"
SUB="$(printf '%s' "$STATE" | grep -o '"id":"sub_[A-Za-z0-9]*"' | head -n1 | sed -E 's/.*"(sub_[A-Za-z0-9]*)"/\1/')"
[ -n "$SUB" ] || fail "no simulated subscription for the user: $STATE"
curl -sS --fail-with-body -X POST "${STAFF[@]}" -H 'Content-Type: application/json' \
  -d '{"outcome":"fail"}' "${BILLING_URL}/billing/v1/sim/subscriptions/${SUB}/next-charge" >/dev/null
curl -sS --fail-with-body -X POST "${STAFF[@]}" "${BILLING_URL}/billing/v1/sim/subscriptions/${SUB}/renew-now" >/dev/null
wait_for payment_failed 60 || fail "no payment_failed email"
echo "   payment_failed email written"

echo "== 3. wait for the grace period (GRACE_PERIOD_SECONDS=60 on the worker) to run out"
wait_for subscription_suspended "$GRACE_WAIT" ||
  fail "no subscription_suspended email in ${GRACE_WAIT}s (worker needs GRACE_PERIOD_SECONDS=60 and SIM_FAST_JOBS=true)"
echo "   subscription_suspended email written"

if [ -n "${PSQL:-}" ]; then
  echo "== 4. deliver every simulated webhook a second time"
  $PSQL -c "UPDATE billing_sim.sim_webhook_outbox SET status='pending', delivered_at=NULL, deliver_after=now() WHERE status='delivered'" >/dev/null
  sleep 15
fi

echo "== the emails for ${EMAIL}"
for f in "$OUTBOX"/*.eml; do
  grep -q "^To: .*${EMAIL}" "$f" || continue
  printf '   %s  [%s]  %s\n' "$f" "$(grep -m1 '^X-GranthanAI-Template:' "$f" | cut -d' ' -f2)" \
    "$(grep -m1 '^Subject:' "$f" | cut -c10-)"
done
for t in receipt payment_failed subscription_suspended; do
  [ "$(eml_count "$t")" = 1 ] || fail "expected exactly one $t email, found $(eml_count "$t")"
done
grep -q 'filename="SI-2627-' "$OUTBOX"/*.eml || fail "the receipt does not carry the SI invoice PDF"
echo "OK: exactly one receipt (SI PDF attached), one payment_failed and one subscription_suspended"
echo "    open the files in ${OUTBOX} by double-clicking them"
