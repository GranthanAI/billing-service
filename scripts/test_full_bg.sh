#!/usr/bin/env bash
# `make test-all` in the background. Returns at once; when the run ends,
# reports/test-run-latest.txt holds the summary, the failures and the slowest tests.
# Works in Git Bash on Windows.
set -u
cd "$(dirname "$0")/.."
mkdir -p reports
FULL=reports/test-run-latest.full.log
OUT=reports/test-run-latest.txt
echo "running (started $(date '+%Y-%m-%d %H:%M:%S'))" >"$OUT"
(
  start=$(date +%s)
  make test-all >"$FULL" 2>&1
  code=$?
  {
    echo "make test-all exit code: $code  ($(($(date +%s) - start)) s)"
    echo
    echo "== summary"
    grep -E "^(=+ )?[0-9]+ (passed|failed)|passed|failed" "$FULL" | grep -E " in [0-9.]+s" | tail -3
    echo
    echo "== failures"
    grep -E "^(FAILED|ERROR) " "$FULL" || echo "none"
    echo
    echo "== slowest tests"
    sed -n '/slowest .* durations/,/^$/p' "$FULL" | head -30
    echo
    echo "== coverage"
    grep -E "^TOTAL|Required test coverage|FAIL Required" "$FULL" || true
  } >"$OUT"
) >/dev/null 2>&1 &
echo "started; read $OUT when it finishes (full log: $FULL)"
