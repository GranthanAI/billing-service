#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s24 Free picks a model that cannot answer: 409 model.unavailable, nothing is answered by another model, no model call
scenario "s24 Unavailable model -> 409 model.unavailable"
need_stack
need_service "conversation-service" "http://127.0.0.1:8002/ready"
[ "$(catalog_available claude-haiku-5-5)" = false ] || skip "claude-haiku-5-5 is reported available (or the availability job has not run): there is no unavailable model to pick"
register_user unavailable || exit 1
api GET /billing/me/models "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'any(m["id"]=="claude-haiku-5-5" and m["allowed"] and m["available"]==False for g in d["groups"] for m in g["models"])' "me/models: Claude Haiku 5.5 is included but available=false"
assert_json "$HTTP_BODY" 'any(m["id"]=="openai/gpt-oss-120b" and m["allowed"] and m["available"] for g in d["groups"] for m in g["models"])' "me/models: GPT-OSS 120B is available"
new_conversation || { fail "could not create a conversation"; exit 1; }
send_message claude-haiku-5-5 "hello"
assert_eq "$HTTP_CODE" 409 "the send is refused with 409"
assert_json "$HTTP_BODY" 'd["detail"]["message_key"]=="model.unavailable" and d["detail"]["message"]=="Claude Haiku 5.5 isn'"'"'t available right now. Please pick another model."' "message_key model.unavailable with the model name"
api GET "/conversations/$CONV_ID/messages?limit=10" "" "$USER_TOKEN"
assert_json "$HTTP_BODY" 'len(d.get("items", []))==0' "nothing was persisted"
me
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["total"]')" 0 "no message was counted"
reconcile_check
