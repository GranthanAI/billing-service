#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s20 Webhook chaos
scenario "s20 Webhook duplicates, drops and reordering"
skip "the simulator in this build has no chaos switch route (its webhook delivery is exercised by tests/integration); nothing to turn on"
