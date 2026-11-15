#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s02 Upgrade Free -> Pro through the simulated checkout, through the gateway
scenario "s02 Free -> Pro: checkout, SI invoice, the picker shows Smart"
need_stack
register_user upgrade || exit 1
api GET /billing/me/models "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'd["plan"]["code"]=="free"' "starts on Free"
become pro || exit 1
api GET /billing/me "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'd["plan"]["code"]=="pro" and d["status"]=="active"' "Pro is active"
api GET /billing/invoices "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'd["invoices"][0]["number"].startswith("SI/")' "the invoice is from the simulated series (SI/...)"
api GET /billing/me/models "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'all(m["allowed"] for g in d["groups"] if g["group"]=="smart" for m in g["models"])' "every Smart model is included on Pro"
assert_json "$HTTP_BODY" 'next(g for g in d["groups"] if g["group"]=="best")["daily_limit"]==5' "Advanced shows 5 a day on Pro"
reconcile_check
