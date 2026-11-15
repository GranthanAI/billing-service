#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s23 The 30-messages-a-minute rule applies to every plan (reserve-only, like B4f)
scenario "s23 30 messages a minute: the 31st says limit.per_minute"
need_stack
register_user perminute || exit 1
become pro || exit 1
step "a burst at the start of a clock minute"
align_minute
OUT="$(reserve "$USER_ID" openai/gpt-oss-20b 31)"
assert_eq "$(count_where "$OUT" 'd["allowed"]')" 30 "30 reserves allowed in the minute"
assert_json "$(printf '%s\n' "$OUT" | last_line)" 'not d["allowed"] and d["message_key"]=="limit.per_minute"' "the 31st is refused with limit.per_minute"
me
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["total"]')" 30 "the refused one used no daily message"
step "the next minute is allowed again"
S="$(date +%S)"; sleep $((61 - 10#$S))
assert_json "$(reserve "$USER_ID" openai/gpt-oss-20b 1)" 'd["allowed"]' "a reserve in the next minute is allowed"
reconcile_check
