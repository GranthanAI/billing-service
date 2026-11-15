#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s09 Downgrade Max -> Pro: Max until the period ends, then Pro
scenario "s09 Max -> Pro at the period end"
need_stack
register_user downpro || exit 1
become max || exit 1
api POST /billing/subscription/change '{"plan_code":"pro","interval":"month"}' "$USER_TOKEN"
OPTIONS="$HTTP_BODY"
if [ -n "$(jget "$OPTIONS" 'd.get("simulator_url")')" ]; then sim_pay "$OPTIONS" /checkout/verify || fail "authorising the Pro mandate"; fi
me
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" max "still Max until the period ends"
assert_json "$HTTP_BODY" 'd["pending_change"] and d["pending_change"]["plan_code"]=="pro"' "a change to Pro is scheduled"
SUB="$(sub_id)"
sim POST "/subscriptions/$SUB/renew-now"
wait_me 60 'd["plan"]["code"]' = pro
me
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" pro "Pro after the renewal"
reconcile_check
