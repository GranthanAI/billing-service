#!/usr/bin/env bash
# One line per shared dev service (port, PID, health), then the service-token and gateway-secret checks against the
# running billing, then Kafka. Read only: starts and stops nothing.   bash scripts/dev_status.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PY=".venv/Scripts/python"; [ -x "$PY" ] || PY=".venv/bin/python"

pid_of() {  # first LISTENING pid on a port (Windows netstat, else ss)
  if command -v netstat >/dev/null 2>&1 && netstat -ano 2>/dev/null | grep -q LISTENING; then
    netstat -ano | grep LISTENING | grep -E "[:.]$1 " | awk '{print $NF}' | sort -u | head -1
  else
    ss -ltnp 2>/dev/null | grep -E "[:.]$1 " | sed -E 's/.*pid=([0-9]+).*/\1/' | head -1
  fi
}
code() { curl -s -o /dev/null -w "%{http_code}" --max-time 4 "$1" 2>/dev/null || echo 000; }
fails=0
row() {  # name port url
  local pid status c
  pid="$(pid_of "$2")"
  if [ -z "$pid" ]; then printf '%-14s %-6s %-8s %s\n' "$1" "$2" "-" "DOWN (nothing listening)"; fails=$((fails + 1)); return; fi
  c="$(code "$3")"
  if [ "$c" = 200 ]; then status="healthy (HTTP 200 $3)"; else status="UNHEALTHY (HTTP $c $3)"; fails=$((fails + 1)); fi
  printf '%-14s %-6s %-8s %s\n' "$1" "$2" "$pid" "$status"
}

echo "== services"
printf '%-14s %-6s %-8s %s\n' service port pid health
row auth 8001 http://127.0.0.1:8001/health
row conversation 8002 http://127.0.0.1:8002/ready
row llm 8003 http://127.0.0.1:8003/health
row memory 8004 http://127.0.0.1:8004/health
row file 8006 http://127.0.0.1:8006/health/ready
row gateway 8080 http://127.0.0.1:8080/api/v1/ready
row billing-api 8090 http://127.0.0.1:8090/readyz
row billing-grpc 50061 http://127.0.0.1:8090/healthz
row billing-worker 9100 http://127.0.0.1:9100/metrics

echo
echo "== billing service tokens and the gateway secret (against the RUNNING billing)"
if "$PY" scripts/check_service_tokens.py 2>&1 | grep -E "OK|FAIL|UNAUTH|ERROR|error|mismatch|refused" ; then :; else echo "token check could not run"; fi
"$PY" scripts/check_service_tokens.py >/dev/null 2>&1 || fails=$((fails + 1))

echo
echo "== kafka"
if (exec 3<>/dev/tcp/127.0.0.1/9092) 2>/dev/null; then
  n="$(MSYS_NO_PATHCONV=1 docker exec "${KAFKA_CONTAINER:-graphgpt-kafka}" /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list 2>/dev/null | wc -l | tr -d ' ')"
  want="$(sed -E 's/#.*$//; s/\|.*$//; s/[[:space:]]+//g' scripts/topics.txt | grep -vc '^$')"
  printf 'kafka          9092   reachable; %s topics exist (%s expected by scripts/topics.txt)\n' "$n" "$want"
else
  echo "kafka          9092   NOT reachable"; fails=$((fails + 1))
fi

echo
[ "$fails" -eq 0 ] && echo "ALL OK" || echo "$fails problem(s)"
exit "$fails"
