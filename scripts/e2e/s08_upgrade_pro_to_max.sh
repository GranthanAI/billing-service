#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s08 Upgrade Pro -> Max mid-period: a new checkout, a prorated refund, Advanced goes to 15 a day
scenario "s08 Pro -> Max mid-period"
need_stack
register_user upmax || exit 1
become pro || exit 1
api POST /billing/subscription/change/preview '{"plan_code":"max","interval":"month"}' "$USER_TOKEN"
assert_eq "$HTTP_CODE" 200 "the change preview answers"
api POST /billing/subscription/change '{"plan_code":"max","interval":"month"}' "$USER_TOKEN"
OPTIONS="$HTTP_BODY"
if [ -n "$(jget "$OPTIONS" 'd.get("simulator_url")')" ]; then sim_pay "$OPTIONS" /checkout/verify || fail "paying the Max mandate"; fi
wait_me 60 'd["plan"]["code"]' = max
me
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" max "Max is active"
api GET /billing/me/models "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'next(g for g in d["groups"] if g["group"]=="best")["daily_limit"]==15' "Advanced shows 15 a day"
api GET /billing/invoices "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'any(i["kind"].startswith("credit_note") for i in d["invoices"])' "a credit note was issued for the unused Pro time"
reconcile_check
