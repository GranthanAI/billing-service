#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s10 Cancel and resume before the end
scenario "s10 Cancel, then resume"
need_stack
register_user cancel || exit 1
become pro || exit 1
api POST /billing/subscription/cancel "" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 200 "cancel is accepted"
me
assert_json "$HTTP_BODY" 'd["status"]=="cancelling" or d["cancel_at_period_end"]' "cancelling, still Pro until the end"
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" pro "Pro access continues"
api POST /billing/subscription/resume "" "$USER_TOKEN"
OPTIONS="$HTTP_BODY"
if [ -n "$(jget "$OPTIONS" 'd.get("simulator_url")')" ]; then sim_pay "$OPTIONS" /checkout/verify || fail "paying the new mandate"; fi
wait_me 40 'd["status"]' = active
me
assert_eq "$(jget "$HTTP_BODY" 'd["status"]')" active "active again with the next mandate"
reconcile_check
