#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s04 Renewal success: renew now, the period advances, invoice 000002
scenario "s04 Renewal: period advances and a second invoice is issued"
need_stack
register_user renew || exit 1
staff_token
become pro || exit 1
me; END1="$(jget "$HTTP_BODY" 'd["current_period_end"]')"
SUB="$(sub_id)"; [ -n "$SUB" ] || { fail "no simulator subscription id"; exit 1; }
sim POST "/subscriptions/$SUB/next-charge" '{"outcome":"success"}'
sim POST "/subscriptions/$SUB/renew-now"
wait_me 40 'd["current_period_end"]' != "$END1"
me; END2="$(jget "$HTTP_BODY" 'd["current_period_end"]')"
[ "$END2" != "$END1" ] && pass "the period end moved ($END1 -> $END2)" || fail "the period end did not move"
api GET /billing/invoices "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'len([i for i in d["invoices"] if i["kind"]=="tax_invoice"])>=2' "a second tax invoice exists"
reconcile_check
