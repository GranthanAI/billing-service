#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s18 Billing outage. Stops the shared billing and needs real degraded-mode sends.
scenario "s18 Billing outage / degraded mode"
[ "${E2E_ALLOW_OUTAGE:-}" = 1 ] || skip "stops the SHARED billing-api and needs real sends; set E2E_ALLOW_OUTAGE=1 to run it by hand (scripts/dev_down.sh, then dev_up.sh)"
skip "degraded mode needs real Fast model calls (budget is 3); covered by conversation-service tests/unit/test_billing_send.py"
