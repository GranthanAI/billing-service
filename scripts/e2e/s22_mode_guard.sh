#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s22 Mode guard: billing with PAYMENT_SIMULATION=false on the simulation database refuses to start
scenario "s22 Mode guard refuses to start"
need_service "billing" "$BILLING/readyz"
cd "$BILLING_ROOT" || exit 1
LOG="$(mktemp)"
( set -a; . ./.env.dev; set +a; PAYMENT_SIMULATION=false GRPC_PORT=50999 timeout 60 "$PY_BILLING" -m uvicorn billing.main_api:app --host 127.0.0.1 --port 8199 >"$LOG" 2>&1 ); CODE=$?
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8199/readyz"; then fail "a second billing started on :8199 and is still answering"; else pass "nothing answers on :8199 (the process did not stay up)"; fi
[ "$CODE" != 0 ] && pass "the process exited non-zero ($CODE)" || fail "the process exited 0"
grep -qiE "simulat|gateway mode|refus" "$LOG" && pass "the log names the simulation / mode problem" || fail "no mode message in the log: $(tail -3 "$LOG" | tr '\n' ' ')"
rm -f "$LOG"
