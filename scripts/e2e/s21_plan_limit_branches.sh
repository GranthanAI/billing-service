#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s21 Plan limit: branches per tree. Free allows 20; the 21st is refused with limit.branches_per_tree and everything stays readable.
scenario "s21 Branch limit: new branches refused, existing ones readable"
need_stack
need_service "conversation-service" "http://127.0.0.1:8002/ready"
register_user branches || exit 1
new_conversation || { fail "could not create the root conversation"; exit 1; }
ROOT_ID="$CONV_ID"
LIMIT="$(billing GET /billing/v1/me "" "$USER_ID"; jget "$HTTP_BODY" 'd["limits"]["branches_per_tree"]')"
step "creating $LIMIT branches from the root"
for ((i = 1; i <= LIMIT; i++)); do
  api POST /conversations "{\"title\":\"b$i\",\"parent_conversation_id\":\"$ROOT_ID\"}" "$USER_TOKEN"
  [ "$HTTP_CODE" = 201 ] || [ "$HTTP_CODE" = 200 ] || { fail "branch $i -> $HTTP_CODE $HTTP_BODY"; break; }
done
api POST /conversations "{\"title\":\"one too many\",\"parent_conversation_id\":\"$ROOT_ID\"}" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 403 "the next branch is refused"
assert_json "$HTTP_BODY" 'd["detail"]["message_key"]=="limit.branches_per_tree" and d["detail"]["reason"]=="plan_limit"' "message_key is limit.branches_per_tree"
api GET "/conversations/$ROOT_ID" "" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 200 "existing conversations stay readable"
