#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s13 An explicit Smart model never consumes Advanced
scenario "s13 Max: 20 explicit Smart messages leave Advanced at 0 used"
need_stack
register_user smart_only || exit 1
become max || exit 1
OUT="$(reserve "$USER_ID" openai/gpt-oss-120b 20)"
assert_eq "$(count_where "$OUT" 'd["allowed"] and d["group"]=="smart"')" 20 "20 reserves granted on Smart"
me
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["best"]')" 0 "the Advanced counter stays 0"
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["total"]')" 20 "the daily total is 20"
reconcile_check
