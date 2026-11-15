#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s12 Max: 15 Advanced answers, then Smart (reserve-only)
scenario "s12 Max: 15 Advanced, the 16th continues on Smart"
need_stack
[ "$(catalog_available claude-sonnet-5-5)" = true ] || skip "claude-sonnet-5-5 is not available here (llm-service has no Anthropic key); the allowance logic is covered by tests/integration/test_hot_path.py"
register_user max_adv || exit 1
become max || exit 1
OUT="$(reserve "$USER_ID" claude-sonnet-5-5 16)"
assert_eq "$(count_where "$OUT" 'd["allowed"] and d["group"]=="best" and not d["downgraded"]')" 15 "15 Advanced reserves granted on Advanced"
assert_json "$(printf '%s\n' "$OUT" | last_line)" 'd["allowed"] and d["downgraded"] and d["group"]=="smart" and d["message_key"]=="limit.best_switched"' "the 16th continues on Smart with limit.best_switched"
reconcile_check
