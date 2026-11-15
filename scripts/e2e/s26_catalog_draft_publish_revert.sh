#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s26 Catalog: a draft that is a RISE, publish, /plans shows it, then revert with apply-limits
scenario "s26 Catalog draft -> publish -> /plans -> revert"
need_stack
staff_token || exit 1
admin GET /catalog
[ "$HTTP_CODE" = 200 ] || { fail "GET /catalog -> $HTTP_CODE $HTTP_BODY"; exit 1; }
ORIG="$HTTP_BODY"
OLD="$(jget "$ORIG" '[p for p in d["catalog"]["plans"] if p["code"]=="pro"][0]["limits"]["messages_per_day"]')"
STAMP="$(date +%s)"
mk() { # <version> <messages_per_day>  -> draft body (the active catalog with Pro changed)
  printf '%s' "$ORIG" | python -c "import json,sys
d=json.loads(sys.stdin.read())
c=d['catalog']; c['catalog_version']='$1'
for p in c['plans']:
    if p['code']=='pro': p['limits']['messages_per_day']=$2
print(json.dumps({'catalog':c,'note':'e2e $1'}))"
}
step "draft: Pro $OLD -> $((OLD + 1)) a day (a rise)"
admin POST /catalog/drafts "$(mk "e2e-up-$STAMP" $((OLD + 1)))"
assert_eq "$HTTP_CODE" 201 "the draft is created"
DRAFT="$(jget "$HTTP_BODY" 'd["draft_id"]')"
assert_json "$HTTP_BODY" 'any(l["direction"]=="up" for p in d["diff"]["plans"] for l in p["limits"]) and not any(l["direction"]=="down" for p in d["diff"]["plans"] for l in p["limits"])' "the preview shows only a rise"
admin POST "/catalog/drafts/$DRAFT/publish" '{"reason":"e2e rise"}'
assert_eq "$HTTP_CODE" 200 "published"
api GET /billing/plans ""
assert_eq "$(jget "$HTTP_BODY" '[p for p in d["plans"] if p["code"]=="pro"][0]["limits"]["messages_per_day"]')" $((OLD + 1)) "/plans shows the new number"
step "revert: back to $OLD (a fall: existing subscribers keep the old limit until it is applied)"
admin POST /catalog/drafts "$(mk "e2e-down-$STAMP" "$OLD")"
DRAFT2="$(jget "$HTTP_BODY" 'd["draft_id"]')"
admin POST "/catalog/drafts/$DRAFT2/publish" '{"reason":"e2e revert"}'
assert_eq "$HTTP_CODE" 200 "the revert is published"
AUDIT="$(jget "$HTTP_BODY" '(d.get("apply_limits") or [{}])[0].get("audit_id")')"
if [ -n "$AUDIT" ]; then
  admin POST "/catalog/apply-limits/$AUDIT" '{"reason":"e2e revert"}'
  assert_eq "$HTTP_CODE" 200 "the lowered limits are applied"
else
  pass "no apply-limits step was needed (no subscriber was above the old limit)"
fi
api GET /billing/plans ""
assert_eq "$(jget "$HTTP_BODY" '[p for p in d["plans"] if p["code"]=="pro"][0]["limits"]["messages_per_day"]')" "$OLD" "/plans is back to the original number"
reconcile_check
