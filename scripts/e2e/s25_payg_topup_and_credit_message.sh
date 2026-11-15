#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s25 Pay as you Go: top up through the simulator, then a message beyond the plan is paid from credits (reserve-only)
scenario "s25 PAYG top-up and a credit-funded message"
need_stack
api GET /billing/plans ""
[ "$(jget "$HTTP_BODY" 'any(p["code"]=="payg" for p in d["plans"])')" = True ] || skip "Pay as you Go is not enabled (PAYG_ENABLED=false in billing)"
register_user payg || exit 1
PACK="$(jget "$HTTP_BODY" '[p for p in d["plans"] if p["code"]=="payg"][0]["payg"]["topup_packs"][0]["code"]')"
api POST /billing/wallet/topups "{\"pack_code\":\"$PACK\",\"state_code\":\"10\",\"email\":\"$USER_EMAIL\"}" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 201 "the top-up order is created"
sim_pay "$HTTP_BODY" /wallet/topups/verify || fail "paying the top-up"
api GET /billing/wallet "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'd["wallet"]["balance_paise"]>0' "the wallet has a balance"
step "a Smart model the Free plan does not include is paid from credits"
OUT="$(reserve "$USER_ID" gemini-2.5-flash 1)"
assert_json "$OUT" 'd["allowed"] and d["funded_by"]=="credits" and d["group"]=="smart"' "allowed, funded_by credits"
api GET /billing/wallet "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'd["wallet"]["held_paise"]>0 or d["wallet"]["balance_paise"]>0' "the credits are held for the message"
reconcile_check
