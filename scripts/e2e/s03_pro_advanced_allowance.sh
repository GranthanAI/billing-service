#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s03 Pro picks Claude Sonnet: allowed (5 Advanced a day), the 6th continues on Smart (reserve-only, no 6 real calls)
scenario "s03 Pro: 5 Advanced answers, the 6th continues on Smart"
need_stack
[ "$(catalog_available claude-sonnet-5-5)" = true ] || skip "claude-sonnet-5-5 is not available here (llm-service has no Anthropic key); the allowance logic is covered by tests/integration/test_hot_path.py"
register_user pro_adv || exit 1
become pro || exit 1
OUT="$(reserve "$USER_ID" claude-sonnet-5-5 6)"
assert_eq "$(count_where "$OUT" 'd["allowed"] and d["group"]=="best" and not d["downgraded"]')" 5 "5 Advanced reserves granted on Advanced"
assert_json "$(printf '%s\n' "$OUT" | last_line)" 'd["allowed"] and d["downgraded"] and d["group"]=="smart" and d["message_key"]=="limit.best_switched" and d["model"]!="claude-sonnet-5-5"' "the 6th is allowed, downgraded, granted on a Smart model"
reconcile_check
