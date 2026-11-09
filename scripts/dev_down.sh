#!/usr/bin/env bash
# Stops exactly the processes scripts/dev_up.sh started (the PIDs in .dev/*.pid, with their child processes).
# It never kills anything by name or by port.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

stop_one() { # name
  local f=".dev/$1.pid"
  if [ ! -f "$f" ]; then
    echo "$1: no pid file (not started by dev_up)"
    return
  fi
  local pid; pid="$(tr -d '\r\n' < "$f")"
  if tasklist //FI "PID eq $pid" 2>/dev/null | grep -q " $pid "; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 && echo "$1: stopped pid $pid (and its children)" || echo "$1: could not stop pid $pid" >&2
  else
    echo "$1: pid $pid was not running"
  fi
  rm -f "$f"
}

stop_one worker
stop_one api

for port in 8090 50061 9100; do
  if netstat -ano 2>/dev/null | tr -d '\r' | awk -v p=":$port" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING"' | grep -q .; then
    echo "port $port is still listening (not ours to kill: another process holds it)"
  fi
done
