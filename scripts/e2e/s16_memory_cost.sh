#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s16 Memory cost: 20 messages in one conversation -> one aux memory.summary row. Needs 20 real model calls.
scenario "s16 Memory summary cost"
skip "needs 20 real model calls in one conversation; the suite budget is 3 real calls (E2E_MAX_REAL_CALLS). Run by hand with a larger budget"
