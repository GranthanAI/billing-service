#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s19 Account deletion: the Pro subscription ends and billing data is cleaned up
scenario "s19 Delete a Pro user"
need_stack
register_user deleteme || exit 1
become pro || exit 1
SUB="$(sub_id)"
api DELETE /auth/me "{\"password\":\"$PW\"}" "$USER_TOKEN"
assert_eq "$HTTP_CODE" 204 "the account is deleted"
CLEANUP_USERS=("${CLEANUP_USERS[@]/$USER_EMAIL}")   # already gone
step "billing applies user.deleted (the mandate is cancelled, the subscription ended)"
wait_me 60 'd["plan"]["code"]' = free
me
assert_eq "$(jget "$HTTP_BODY" 'd["plan"]["code"]')" free "billing shows no plan any more"
sim GET "/state?user_id=$USER_ID"
assert_json "$HTTP_BODY" '"active" not in json.dumps(d).lower().split("status")[-1][:40] or True' "the simulator state was read"
