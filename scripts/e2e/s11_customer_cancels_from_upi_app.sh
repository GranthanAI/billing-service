#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s11 The customer cancels from the bank / UPI app
scenario "s11 Customer cancel from the UPI app"
need_stack
register_user upicancel || exit 1
become pro || exit 1
SUB="$(sub_id)"
sim POST "/subscriptions/$SUB/customer-cancel"
wait_me 40 'd["status"]' = cancelling
me
assert_eq "$(jget "$HTTP_BODY" 'd["status"]')" cancelling "cancelling"
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" pro "access continues to the period end"
reconcile_check
