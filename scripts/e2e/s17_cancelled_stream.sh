#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s17 Cancelled stream: release and count. Needs a live streamed model call.
scenario "s17 Cancelled streams"
skip "needs real streamed model calls cancelled mid-flight (budget is 3 real calls); the release path is covered by conversation-service tests/unit/test_billing_send.py"
