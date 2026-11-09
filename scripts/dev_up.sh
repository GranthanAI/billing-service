#!/usr/bin/env bash
# Starts the SHARED dev billing (simulation): API on 127.0.0.1:8090 (REST) and :50061 (gRPC), worker on :9100 (health/metrics).
# One instance serves every repo's integration tests: other repos must NOT start their own billing.
#
#   scripts/dev_up.sh     start (refuses if a port is taken, and says who has it)
#   scripts/dev_down.sh   stop exactly what dev_up started
#
# Needs (already running, shared): graphgpt-postgres (5433), billing-valkey (6380), graphgpt-kafka (127.0.0.1:9092).
# Git Bash on Windows. Logs: .dev/logs/*.log   PIDs: .dev/*.pid   Env: .env.dev (created from .env.example on first run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "no .venv found: run 'uv sync' first" >&2; exit 1; }
WIN_PY="$(cygpath -w "$PY" 2>/dev/null || echo "$PY")"
WIN_ROOT="$(cygpath -w "$ROOT" 2>/dev/null || echo "$ROOT")"
mkdir -p .dev/logs

REST_PORT=8090
GRPC_PORT_DEV=50061
WORKER_PORT=9100

listener_pid() { # PID listening on a TCP port (empty if none)
  netstat -ano 2>/dev/null | tr -d '\r' | awk -v p=":$1" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING" {print $5; exit}'
}
describe_pid() {
  powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"ProcessId=$1\" | ForEach-Object { 'pid ' + \$_.ProcessId + ' started ' + \$_.CreationDate + ' : ' + \$_.CommandLine }" 2>/dev/null | tr -d '\r' | cut -c1-220
}
alive() { tasklist //FI "PID eq $1" 2>/dev/null | grep -q " $1 "; }

# --- 1. already ours? ------------------------------------------------------------------------------------------------
if [ -f .dev/api.pid ] && alive "$(cat .dev/api.pid)"; then
  echo "The shared dev billing is already running (api pid $(cat .dev/api.pid)). Use scripts/dev_down.sh to stop it."
  exit 0
fi
rm -f .dev/api.pid .dev/worker.pid

# --- 2. ports must be free ---------------------------------------------------------------------------------------------
busy=0
for port in "$REST_PORT" "$GRPC_PORT_DEV" "$WORKER_PORT"; do
  pid="$(listener_pid "$port")"
  if [ -n "$pid" ]; then
    echo "PORT $port is taken by:" >&2
    describe_pid "$pid" >&2
    busy=1
  fi
done
if [ "$busy" = 1 ]; then
  echo "Refusing to start: free those ports first (or stop the owner). Nothing was changed." >&2
  exit 1
fi

# --- 3. .env.dev ---------------------------------------------------------------------------------------------------------
if [ ! -f .env.dev ]; then
  gw="dev-gateway-$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
  tok() { echo "dev-$1-$("$PY" -c 'import secrets; print(secrets.token_hex(12))')"; }
  {
    sed -E 's/^EMAIL_FROM=(.*)$/EMAIL_FROM="\1"/' .env.example     # EMAIL_FROM has <> in it: quote it so the file can be sourced
    cat <<ENV

# ===== shared dev stack overrides (scripts/dev_up.sh); later lines win =====
ENVIRONMENT=development
PAYMENT_SIMULATION=true
SIM_MONTH_SECONDS=600
SIM_FAST_JOBS=true
GRACE_PERIOD_SECONDS=300
EMAIL_MODE=file
EMAIL_OUTBOX_DIR=./.dev/email-outbox
KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092
BILLING_DATABASE_URL=postgresql+asyncpg://billing_app:billing_app@127.0.0.1:5433/billing_db
BILLING_MIGRATION_DATABASE_URL=postgresql+asyncpg://billing_migrator:billing_migrator@127.0.0.1:5433/billing_db
BILLING_VALKEY_URL=redis://127.0.0.1:6380/0
GRPC_PORT=$GRPC_PORT_DEV
FRONTEND_ORIGIN=http://localhost:5173
SIM_WEBHOOK_URL=http://127.0.0.1:8080/api/v1/billing/webhooks/razorpay
# api-gateway sends this as X-Gateway-Secret (its BILLING_GATEWAY_SECRET)
GATEWAY_SHARED_SECRET=$gw
# one gRPC service token per calling service (each service puts its own in its .env)
CONV_BILLING_TOKEN=$(tok conversation)    # conversation-service
FILE_BILLING_TOKEN=$(tok file)            # file-service
LLM_BILLING_TOKEN=$(tok llm)              # llm-service (BILLING_SERVICE_TOKEN)
MEM_BILLING_TOKEN=$(tok memory)           # memory-service
SERVICE_TOKEN_KEYS=\${CONV_BILLING_TOKEN},\${FILE_BILLING_TOKEN},\${LLM_BILLING_TOKEN},\${MEM_BILLING_TOKEN}
ENV
  } > .env.dev
  echo "created .env.dev"
fi
set -a
# shellcheck disable=SC1091
. ./.env.dev
set +a
export PYTHONUNBUFFERED=1

# --- 4. migrations and catalog (simulation) ----------------------------------------------------------------------------------
echo "== alembic upgrade head";   "$PY" -m alembic upgrade head 2>&1 | tail -2
echo "== pricing seed";           "$PY" -m billing.cli pricing seed 2>&1 | tail -2
echo "== catalog sync (simulation)"; "$PY" -m billing.cli catalog sync 2>&1 | tail -3

# --- 5. start API and worker (hidden windows, logs to .dev/logs) ---------------------------------------------------------------
launch() { # name, then the python arguments; writes the Windows PID of the launched process to .dev/<name>.pid
  # The child must not inherit this shell's stdout (a `$(...)` would then wait for it to exit), so nothing is read from
  # PowerShell's stdout: the PID goes to a file.
  local name="$1"; shift
  local log; log="$(cygpath -w "$ROOT/.dev/logs/$name.log")"
  local err; err="$(cygpath -w "$ROOT/.dev/logs/$name.err.log")"
  local pidfile; pidfile="$(cygpath -w "$ROOT/.dev/$name.pid")"
  powershell.exe -NoProfile -Command "\$p = Start-Process -FilePath '$WIN_PY' -ArgumentList '$*' -WorkingDirectory '$WIN_ROOT' -RedirectStandardOutput '$log' -RedirectStandardError '$err' -WindowStyle Hidden -PassThru; Set-Content -NoNewline -Path '$pidfile' -Value \$p.Id" >/dev/null 2>&1 </dev/null
}
launch api -m uvicorn billing.main_api:app --host 127.0.0.1 --port $REST_PORT
launch worker -m billing.main_worker
api_pid="$(cat .dev/api.pid)"; worker_pid="$(cat .dev/worker.pid)"
echo "started api pid $api_pid, worker pid $worker_pid"

wait_http() { # url, seconds
  local i
  for i in $(seq 1 "$2"); do
    curl -fsS --max-time 2 "$1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
if ! wait_http "http://127.0.0.1:$REST_PORT/readyz" 90 || ! wait_http "http://127.0.0.1:$WORKER_PORT/healthz" 90; then
  echo "billing did not become ready: see .dev/logs/api.err.log and worker.err.log" >&2
  tail -5 .dev/logs/api.err.log .dev/logs/worker.err.log 2>/dev/null >&2 || true
  exit 1
fi

# --- 6. summary ---------------------------------------------------------------------------------------------------------------------
echo
echo "billing (simulation) is up. SHARED instance: do not start another billing."
echo "  REST   http://127.0.0.1:$REST_PORT     gRPC 127.0.0.1:$GRPC_PORT_DEV     worker http://127.0.0.1:$WORKER_PORT"
echo "  healthz: $(curl -fsS http://127.0.0.1:$REST_PORT/healthz)"
echo
echo "api-gateway   BILLING_GATEWAY_SECRET=$GATEWAY_SHARED_SECRET   BILLING_SERVICE_URL=http://127.0.0.1:$REST_PORT"
echo "conversation  CONV_BILLING_TOKEN=$CONV_BILLING_TOKEN"
echo "file          FILE_BILLING_TOKEN=$FILE_BILLING_TOKEN"
echo "llm           LLM_BILLING_TOKEN=$LLM_BILLING_TOKEN   (BILLING_SERVICE_TOKEN; BILLING_GRPC_TARGET=127.0.0.1:$GRPC_PORT_DEV)"
echo "memory        MEM_BILLING_TOKEN=$MEM_BILLING_TOKEN"
echo "all services  BILLING_GRPC_TARGET=127.0.0.1:$GRPC_PORT_DEV   KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092"
echo "logs: .dev/logs/   stop: scripts/dev_down.sh"
