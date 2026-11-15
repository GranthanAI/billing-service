#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s14 YouTube monthly allowance (reserve-only): Free has 3 a month, the 4th is refused
scenario "s14 Free: 3 YouTube videos a month, the 4th says limit.monthly"
need_stack
register_user yt || exit 1
OUT="$(reserve "$USER_ID" - 4 youtube)"
assert_eq "$(count_where "$OUT" 'd["allowed"]')" 3 "3 videos reserved"
assert_json "$(printf '%s\n' "$OUT" | last_line)" 'not d["allowed"] and d["message_key"]=="limit.monthly"' "the 4th is refused with limit.monthly"
reconcile_check
