#!/usr/bin/env bash
# Shared helpers for the end-to-end scenarios (Git Bash on Windows; also works on Linux/macOS with a venv layout).
# Source it from a scenario:   source "$(dirname "$0")/lib.sh"; scenario "s01 Free daily messages" ...
#
# What it gives a scenario:
#   register_user <tag>        a throwaway user through the gateway (register -> activate -> login): USER_ID USER_EMAIL USER_TOKEN
#   staff_token                a throwaway staff user (role billing_admin): STAFF_TOKEN (falls back to direct staff headers)
#   api METHOD PATH [BODY] [TOKEN]   a gateway call; sets HTTP_CODE HTTP_BODY (writes get an Idempotency-Key)
#   billing METHOD PATH [BODY] [USER_ID]   billing direct (dev): as a user (X-User-Id) or as staff when USER_ID is "staff"
#   sim METHOD PATH [BODY]     simulator controls (staff, direct)
#   become <plan> [interval]   the real checkout flow against the simulator, through the gateway
#   reserve <user> <model|-> <n> [message|youtube|manim]   reserve-only calls to billing gRPC (no model call); JSON lines
#   jget <json> <python expr on d>   print a value;   assert_eq / assert_json / assert_true   PASS/FAIL lines
#   wait_until <seconds> <command...>
#   reconcile_check            reconciliation for today: every check must show difference 0 (the ledger balances)
#   align_minute               wait for the start of a clock minute (the per-minute rule counts calendar minutes)
#   real_call_allowed          1 of at most E2E_MAX_REAL_CALLS (default 3) real model calls for the whole suite
# Output: PASS / FAIL / SKIP lines, then "RESULT <scenario> PASS|FAIL|SKIP". Exit code 0 unless something FAILed.
# Every throwaway user is deleted in an EXIT trap (also after a failure or Ctrl-C).

set -uo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BILLING_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
STACK_ROOT="$(cd "$BILLING_ROOT/.." && pwd)"
GW="${GW:-http://127.0.0.1:8080/api/v1}"
BILLING="${BILLING:-http://127.0.0.1:8090}"
PW="${E2E_PASSWORD:-E2eCheck#2026x}"
E2E_MAX_REAL_CALLS="${E2E_MAX_REAL_CALLS:-3}"
E2E_REAL_FILE="${E2E_REAL_FILE:-${TMPDIR:-/tmp}/e2e-real-calls.count}"

venv_py() { # <repo> -> that repo's venv python (Windows Scripts/ or bin/)
  local p="$STACK_ROOT/$1/.venv/Scripts/python.exe"
  [ -x "$p" ] || p="$STACK_ROOT/$1/.venv/bin/python"
  echo "$p"
}
PY_AUTH="$(venv_py auth-service)"
PY_CONV="$(venv_py conversation-service)"
PY_BILLING="$(venv_py billing-service)"
if [ ! -x "$PY_BILLING" ]; then PY_BILLING="$BILLING_ROOT/.venv/Scripts/python.exe"; fi

GS="${GATEWAY_SECRET:-$(grep -E '^GATEWAY_SHARED_SECRET=' "$BILLING_ROOT/.env.dev" 2>/dev/null | head -1 | cut -d= -f2-)}"

SCENARIO="${SCENARIO:-$(basename "${0:-scenario}" .sh)}"
PASSES=0
FAILS=0
CLEANUP_USERS=()   # "email" entries; the password is the same for all
BILLING_ONLY_IDS=()
HTTP_CODE=""
HTTP_BODY=""

scenario() { SCENARIO="$1"; echo "=== $1"; }
step() { echo "--- $*"; }
pass() { PASSES=$((PASSES + 1)); echo "PASS $SCENARIO: $*"; }
fail() { FAILS=$((FAILS + 1)); echo "FAIL $SCENARIO: $*"; }
# skip: this scenario cannot run here (something it needs is not running). Not a failure.
skip() { echo "SKIP $SCENARIO: $*"; echo "RESULT $SCENARIO SKIP"; SKIPPED=1; exit 0; }

jget() { # <json> <python expression using d>
  printf '%s' "$1" | python -c "import json,sys
try:
    d=json.loads(sys.stdin.read())
    v=($2)
    print('' if v is None else v)
except Exception as e:
    print('')" 2>/dev/null
}
assert_eq() { # <actual> <expected> <message>
  if [ "$1" = "$2" ]; then pass "$3 (= $2)"; else fail "$3: expected '$2', got '$1'"; fi
}
assert_true() { # <message> <command...>
  local msg="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$msg"; else fail "$msg"; fi
}
assert_json() { # <json> <python expr that must be truthy> <message>
  local ok
  ok="$(printf '%s' "$1" | python -c "import json,sys
d=json.loads(sys.stdin.read())
print('1' if ($2) else '')" 2>/dev/null)"
  if [ -n "$ok" ]; then pass "$3"; else fail "$3: $2 is false for $(printf '%s' "$1" | cut -c1-300)"; fi
}
wait_until() { # <seconds> <command...>: true as soon as the command succeeds
  local limit="$1"; shift
  local i
  for ((i = 0; i < limit; i++)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

# --- services ---------------------------------------------------------------------------------------------------
need_service() { # <name> <url>: SKIP the scenario when it is not answering
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$2" 2>/dev/null || echo 000)"
  [ "$code" = 200 ] || skip "$1 is not running ($2 answered $code)"
}
need_stack() {
  need_service "gateway" "$GW/ready"
  need_service "billing" "$BILLING/readyz"
}

# --- HTTP ---------------------------------------------------------------------------------------------------------
uuid() { python -c "import uuid;print(uuid.uuid4())"; }
api() { # METHOD PATH [BODY] [TOKEN]   (gateway; PATH starts with /)
  local method="$1" path="$2" body="${3:-}" token="${4:-${USER_TOKEN:-}}" out
  out="$(mktemp)"
  local args=(-s -o "$out" -w '%{http_code}' -X "$method" -H 'Content-Type: application/json')
  [ -n "$token" ] && args+=(-H "Authorization: Bearer $token")
  [ "$method" != GET ] && args+=(-H "Idempotency-Key: $(uuid)")
  [ -n "$body" ] && args+=(-d "$body")
  HTTP_CODE="$(curl "${args[@]}" "$GW$path" 2>/dev/null)"
  HTTP_BODY="$(cat "$out")"
  rm -f "$out"
}
billing() { # METHOD PATH [BODY] [USER_ID|staff]   billing direct (dev stack): X-Gateway-Secret + identity headers
  local method="$1" path="$2" body="${3:-}" who="${4:-${USER_ID:-}}" out
  out="$(mktemp)"
  local args=(-s -o "$out" -w '%{http_code}' -X "$method" -H 'Content-Type: application/json' -H "X-Gateway-Secret: $GS")
  if [ "$who" = staff ]; then args+=(-H "X-User-Id: 00000000-0000-4000-8000-000000000001" -H "X-Staff-Role: billing_admin")
  else args+=(-H "X-User-Id: $who"); fi
  [ "$method" != GET ] && args+=(-H "Idempotency-Key: $(uuid)")
  [ -n "$body" ] && args+=(-d "$body")
  HTTP_CODE="$(curl "${args[@]}" "$BILLING$path" 2>/dev/null)"
  HTTP_BODY="$(cat "$out")"
  rm -f "$out"
}
sim() { billing "$1" "/billing/v1/sim$2" "${3:-}" staff; }

# --- users ------------------------------------------------------------------------------------------------------
dbq() { "$PY_AUTH" "$E2E_DIR/dbq.py" "$1"; }
login() { # <email> -> sets USER_TOKEN (retries while the auth rate limit answers 429)
  local i
  for ((i = 0; i < 12; i++)); do
    api POST /auth/login "{\"email\":\"$1\",\"password\":\"$PW\"}" ""
    [ "$HTTP_CODE" != 429 ] && break
    sleep 20
  done
  [ "$HTTP_CODE" = 200 ] || return 1
  USER_TOKEN="$(jget "$HTTP_BODY" 'd["access_token"]')"
}
register_user() { # <tag>
  local tag="$1" stamp
  stamp="$(date +%s)$RANDOM"
  USER_EMAIL="e2e_${tag}_${stamp}@example.com"
  api POST /auth/register "{\"email\":\"$USER_EMAIL\",\"password\":\"$PW\",\"full_name\":\"E2E $tag\"}" ""
  [ "$HTTP_CODE" = 201 ] || { fail "register $USER_EMAIL -> $HTTP_CODE $HTTP_BODY"; return 1; }
  USER_ID="$(jget "$HTTP_BODY" 'd["id"]')"
  CLEANUP_USERS+=("$USER_EMAIL")
  # the OTP only exists in the e-mail: for a throwaway dev user mark the row verified
  dbq "update users set status='ACTIVE', is_email_verified=true where email='${USER_EMAIL,,}' returning email" >/dev/null 2>&1
  login "$USER_EMAIL" || { fail "login $USER_EMAIL -> $HTTP_CODE $HTTP_BODY"; return 1; }
  return 0
}
staff_token() {
  local keep_token="${USER_TOKEN:-}" keep_id="${USER_ID:-}" keep_email="${USER_EMAIL:-}"
  register_user staff || return 1
  dbq "update users set role='billing_admin' where email='${USER_EMAIL,,}' returning email" >/dev/null 2>&1
  login "$USER_EMAIL" || true
  STAFF_TOKEN="$USER_TOKEN"; STAFF_ID="$USER_ID"
  USER_TOKEN="$keep_token"; USER_ID="$keep_id"; USER_EMAIL="$keep_email"
  api GET /billing/admin/accounts?q=nobody "" "$STAFF_TOKEN"
  [ "$HTTP_CODE" = 200 ] && STAFF_VIA_GATEWAY=1 || STAFF_VIA_GATEWAY=0
}
admin() { # METHOD PATH [BODY]   staff call: through the gateway with the staff JWT when it works, else direct
  if [ "${STAFF_VIA_GATEWAY:-0}" = 1 ]; then api "$1" "/billing/admin$2" "${3:-}" "$STAFF_TOKEN"
  else billing "$1" "/admin/billing/v1$2" "${3:-}" staff; fi
}
cleanup() {
  local e
  for e in "${CLEANUP_USERS[@]:-}"; do
    [ -z "$e" ] && continue
    if login "$e"; then
      api DELETE /auth/me "{\"password\":\"$PW\"}" "$USER_TOKEN"
      echo "cleanup: deleted $e ($HTTP_CODE)"
    else
      echo "cleanup: could not log in as $e ($HTTP_CODE); delete it by hand"
    fi
  done
  if [ "${#BILLING_ONLY_IDS[@]}" -gt 0 ]; then
    (cd "$BILLING_ROOT" && uv run --quiet python "$E2E_DIR/userdel.py" "${BILLING_ONLY_IDS[@]}" >/dev/null 2>&1)
  fi
  if [ -z "${SKIPPED:-}" ]; then
    local verdict=PASS
    [ "$FAILS" -gt 0 ] && verdict=FAIL
    echo "RESULT $SCENARIO $verdict ($PASSES passed, $FAILS failed)"
    [ "$FAILS" -gt 0 ] && return 1
  fi
  return 0
}
trap 'cleanup; exit $?' EXIT
trap 'echo "interrupted"; exit 130' INT TERM

# --- plans / reserve / reconcile -------------------------------------------------------------------------------
become() { # <plan> [month|year]  the checkout flow against the simulator, through the gateway
  local plan="$1" interval="${2:-month}" out
  need_service "payment simulator" "$BILLING/readyz"
  out="$(USER_JWT="$USER_TOKEN" BASE_URL="${GW%/api/v1}" PLAN="$plan" INTERVAL="$interval" WAIT_SECONDS=40 bash "$BILLING_ROOT/scripts/sim_checkout_demo.sh" 2>&1)"
  if printf '%s' "$out" | grep -q '^OK:'; then pass "checkout to $plan through the simulator"; return 0; fi
  fail "checkout to $plan: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  return 1
}
reserve() { # <user> <model|-> <n> [kind]  JSON lines, one per call (reserve-only: no model is called)
  (cd "$STACK_ROOT/conversation-service" && "$PY_CONV" "$E2E_DIR/reserve.py" "$@" 2>/dev/null | grep '^{')
}
last_line() { tail -n 1; }
count_where() { # <jsonlines> <python expr on d> -> how many lines satisfy it
  printf '%s\n' "$1" | python -c "import json,sys
n=0
for l in sys.stdin:
    l=l.strip()
    if not l: continue
    d=json.loads(l)
    if ($2): n+=1
print(n)"
}
me() { billing GET /billing/v1/me "" "$USER_ID"; }
today_ist() { python -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=5,minutes=30)).date())"; }
reconcile_check() {
  step "reconciliation (today, else yesterday), then the ledger balance"
  local day body
  for day in "$(today_ist)" "$(python -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=5,minutes=30)).date()-datetime.timedelta(days=1))")"; do
    admin GET "/reconciliation?date=$day"
    if [ "$HTTP_CODE" != 200 ]; then fail "reconciliation -> $HTTP_CODE $HTTP_BODY"; return; fi
    body="$HTTP_BODY"
    [ "$(jget "$body" 'len(d["checks"])')" != 0 ] && break
  done
  if [ "$(jget "$body" 'len(d["checks"])')" = 0 ]; then
    echo "SKIP $SCENARIO: reconciliation has no rows yet (reconcile.daily has not run; nothing to compare)"
    return
  fi
  # the ledger-balance check must be clean. The other checks cover the whole day (every user, other test runs): a problem there
  # is printed as a NOTE for a human, it does not fail this scenario.
  assert_json "$body" 'any(c["check"]=="ledger" and c["status"]=="ok" and float(str(c["difference_paise"]))==0 for c in d["checks"])' "the ledger balances (reconciliation check ledger: ok, difference 0)"
  printf '%s' "$body" | python -c "import json,sys
d=json.loads(sys.stdin.read())
for c in d['checks']:
    if c['check']!='ledger' and (c['status']!='ok' or float(str(c['difference_paise']))!=0):
        print('NOTE reconciliation %s: %s, difference %s: %s' % (c['check'], c['status'], c['difference_paise'], str(c['details'])[:160].replace(chr(10),' ')))"
}
align_minute() { # the per-minute rule counts calendar minutes: start a burst right after a minute turns
  local s
  s="$(date +%S)"; s=$((10#$s))
  if [ "$s" -gt 25 ]; then sleep $((61 - s)); fi
}
real_call_allowed() { # 0 (true) while the suite still has real model calls left; counts the call
  local n=0
  [ -f "$E2E_REAL_FILE" ] && n="$(cat "$E2E_REAL_FILE")"
  if [ "$n" -ge "$E2E_MAX_REAL_CALLS" ]; then return 1; fi
  echo $((n + 1)) > "$E2E_REAL_FILE"
  return 0
}
new_conversation() { # sets CONV_ID
  api POST /conversations '{"title":"e2e"}' "$USER_TOKEN"
  CONV_ID="$(jget "$HTTP_BODY" 'd.get("conversation_id") or d.get("id")')"
  [ -n "$CONV_ID" ]
}
send_message() { # <model> <text>   POST a message through the gateway (HTTP_CODE / HTTP_BODY)
  api POST "/conversations/$CONV_ID/messages" "{\"content\":\"$2\",\"sender\":\"user\",\"model\":\"$1\"}" "$USER_TOKEN"
}
catalog_available() { # <model id> -> prints true|false|unknown from the public catalog
  local body
  body="$(curl -s "$BILLING/billing/v1/models")"
  jget "$body" 'next((str(m.get("available", True)).lower() for m in d["models"] if m["id"]=="'"$1"'"), "unknown")'
}

sim_pay() { # <checkout/top-up options json> <verify path under /billing>  pays on the simulated page, then verifies
  local url path page pay handler
  url="$(jget "$1" 'd.get("simulator_url")')"
  [ -n "$url" ] || { fail "no simulator_url in: $(printf '%s' "$1" | cut -c1-200)"; return 1; }
  path="${url#/api/v1/billing}"
  page="$GW/billing$path"
  curl -s "$page" | grep -q 'SIMULATED PAYMENT' || { fail "the simulated payment page did not load"; return 1; }
  pay="$GW/billing${path%%\?*}/pay?${path#*\?}"
  handler="$(curl -s -X POST -H 'Content-Type: application/json' -d '{"method":"card","card_number":"4111111111111111","otp":"123456"}' "$pay")"
  [ -n "$(jget "$handler" 'd.get("razorpay_payment_id")')" ] || { fail "simulated payment failed: $handler"; return 1; }
  api POST "/billing$2" "$handler" "$USER_TOKEN"   # the handler fields are exactly the verify body
  [ "$HTTP_CODE" = 200 ] || [ "$HTTP_CODE" = 201 ]
}
sub_id() { # the simulator subscription id of USER_ID
  sim GET "/state?user_id=$USER_ID"
  jget "$HTTP_BODY" 'd.get("subscription",{}).get("id") or (d.get("subscriptions") or [{}])[0].get("id")'
}
status_now() { me; jget "$HTTP_BODY" 'd["status"]'; }
wait_me() { # <seconds> <python expr on /me> <op: = | !=> <value>: poll billing's /me until the comparison holds
  local limit="$1" expr="$2" op="$3" val="$4" i v
  for ((i = 0; i < limit; i++)); do
    me
    v="$(jget "$HTTP_BODY" "$expr")"
    if [ "$op" = "=" ] && [ "$v" = "$val" ]; then return 0; fi
    if [ "$op" = "!=" ] && [ "$v" != "$val" ]; then return 0; fi
    sleep 1
  done
  return 1
}
