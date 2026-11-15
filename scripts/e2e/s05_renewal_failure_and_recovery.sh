#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s05 Renewal failure and recovery: past_due with a Pay now link, then active again
scenario "s05 Renewal fails -> past_due -> recovers"
need_stack
register_user renewfail || exit 1
staff_token
become pro || exit 1
SUB="$(sub_id)"; [ -n "$SUB" ] || { fail "no simulator subscription id"; exit 1; }
step "the next charge fails"
sim POST "/subscriptions/$SUB/next-charge" '{"outcome":"fail"}'
sim POST "/subscriptions/$SUB/renew-now"
wait_me 40 'd["status"]' = past_due
me
assert_eq "$(jget "$HTTP_BODY" 'd["status"]')" past_due "the subscription is past_due"
assert_json "$HTTP_BODY" 'bool(d.get("payment_action_url"))' "a Pay now link is offered"
assert_json "$HTTP_BODY" 'd["limits"]["messages_per_day"]>=25' "the plan limits are still served during the grace period"
step "the next charge succeeds"
sim POST "/subscriptions/$SUB/next-charge" '{"outcome":"success"}'
sim POST "/subscriptions/$SUB/renew-now"
wait_me 40 'd["status"]' = active
me
assert_eq "$(jget "$HTTP_BODY" 'd["status"]')" active "active again"
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" pro "still on Pro"
reconcile_check
