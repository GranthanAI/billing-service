#!/usr/bin/env bash
# Runs the chosen scenarios one after the other (default: all) and writes reports/e2e-latest.txt.
#   scripts/e2e/run.sh                 every scenario
#   scripts/e2e/run.sh s01 s02 s23     only these (a prefix of the file name is enough)
# At most E2E_MAX_REAL_CALLS (3) real model calls in the whole run, counted in a shared file; everything else is reserve-only.
# Each scenario deletes its own throwaway users. Exit code 1 when any scenario FAILed (SKIP is not a failure).
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
REPORT="$ROOT/reports/e2e-latest.txt"
mkdir -p "$ROOT/reports"
export E2E_REAL_FILE="${TMPDIR:-/tmp}/e2e-real-calls.$$"
rm -f "$E2E_REAL_FILE"

if [ "$#" -gt 0 ]; then
  LIST=()
  for want in "$@"; do
    found=0
    for f in "$DIR"/${want}*.sh; do [ -e "$f" ] && { LIST+=("$f"); found=1; }; done
    [ "$found" = 1 ] || echo "no scenario matches '$want'" >&2
  done
else
  LIST=("$DIR"/s[0-9][0-9]_*.sh)
fi

{
  echo "e2e run $(date '+%Y-%m-%d %H:%M:%S')  scenarios: ${#LIST[@]}  real model calls allowed: ${E2E_MAX_REAL_CALLS:-3}"
  echo
} > "$REPORT"
STATUS=0
SUMMARY=()
for s in "${LIST[@]}"; do
  name="$(basename "$s" .sh)"
  started=$SECONDS
  out="$(timeout 175 bash "$s" 2>&1)"
  code=$?
  [ "$code" = 124 ] && out="$out"$'\n'"FAIL $name: timed out after 175 seconds"$'\n'"RESULT $name FAIL (timeout)"
  echo "$out" | tee -a "$REPORT"
  echo | tee -a "$REPORT" >/dev/null
  res="$(printf '%s\n' "$out" | grep -E '^RESULT ' | tail -1)"
  [ -n "$res" ] || res="RESULT $name FAIL (no result line)"
  SUMMARY+=("$res  ($((SECONDS - started))s)")
  case "$res" in *FAIL*) STATUS=1 ;; esac
done
{
  echo "=== summary"
  printf '%s\n' "${SUMMARY[@]}"
} | tee -a "$REPORT"
rm -f "$E2E_REAL_FILE"
exit $STATUS
