#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s06 Failure to suspension: the grace period passes, the plan falls back to Free limits, data intact
scenario "s06 Failed renewals -> suspended, Free limits"
need_stack
GRACE="$(grep -E '^GRACE_PERIOD_SECONDS=' "$BILLING_ROOT/.env.dev" | cut -d= -f2)"
[ -n "$GRACE" ] && [ "$GRACE" -le 120 ] || skip "needs GRACE_PERIOD_SECONDS<=120 in billing .env.dev (now '${GRACE:-unset}') so the run fits in 3 minutes; set it and restart billing"
register_user suspend || exit 1
become pro || exit 1
SUB="$(sub_id)"
sim POST "/subscriptions/$SUB/next-charge" '{"outcome":"fail"}'
sim POST "/subscriptions/$SUB/renew-now"
step "waiting for the grace period (${GRACE}s) to pass"
wait_me $((GRACE + 60)) 'd["status"]' = suspended
me
assert_eq "$(jget "$HTTP_BODY" 'd["status"]')" suspended "suspended"
assert_eq "$(jget "$HTTP_BODY" 'd["limits"]["messages_per_day"]')" 25 "Free limits apply (25 a day)"
api GET /conversations "" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 200 "the user's data is still readable"
reconcile_check
