#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s01 Free daily messages: 25 allowed, the 26th is refused with limit.daily (reserve-only: no model is called)
scenario "s01 Free: 25 messages a day, the 26th says limit.daily"
need_stack
register_user free || exit 1
step "26 explicit Fast reserves"
OUT="$(reserve "$USER_ID" openai/gpt-oss-20b 26)"
assert_eq "$(count_where "$OUT" 'd["allowed"]')" 25 "25 reserves allowed"
LAST="$(printf '%s\n' "$OUT" | last_line)"
assert_json "$LAST" 'not d["allowed"] and d["message_key"]=="limit.daily"' "the 26th is refused with message_key limit.daily"
me
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["total"]')" 25 "today's counter is 25 (the refused one is not counted)"
assert_eq "$(jget "$HTTP_BODY" 'd["limits"]["messages_per_day"]')" 25 "the Free plan says 25 a day"
reconcile_check
