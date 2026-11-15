#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s07 Halt: the provider ends the subscription
scenario "s07 Halt: subscription ends, back to Free"
need_stack
register_user halt || exit 1
become pro || exit 1
SUB="$(sub_id)"
sim POST "/subscriptions/$SUB/next-charge" '{"outcome":"halt"}'
sim POST "/subscriptions/$SUB/renew-now"
wait_me 40 'd["plan"]["code"]' = free
me
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" free "back on Free"
reconcile_check
