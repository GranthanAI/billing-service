"""The lifecycle tests of tests/integration/test_lifecycle.py, run unchanged against the payment
simulator with SIM_MONTH_SECONDS=60 (plan 5.7). The fake and the simulator must give the same
subscription states, invoice counts and ledger balances."""

import pytest

from tests.integration import test_lifecycle as lifecycle
from tests.integration.billing_env import Env

# Written around the fake's "10days" billing period, which the simulator does not have.
NOT_APPLICABLE = {"test_a_40_day_timeline_gives_the_expected_states_events_and_invoices"}


# These depend on real durations: a downgrade needs 10 minutes of lead, a renewal reminder looks
# 24 to 48 hours ahead, and one asserts calendar dates. They run with SIM_MONTH_SECONDS=0.
CALENDAR_MONTHS = {
    "test_a_failed_first_charge_of_the_next_mandate_is_past_due_on_the_new_plan",
    "test_a_halted_next_mandate_ends_the_subscription",
    "test_the_poll_recovers_a_missed_charge",
    "test_renewal_reminder_fires_once_per_period",
    "test_renewal_reminder_skips_cancelling_subscriptions_and_names_a_scheduled_downgrade",
}


def month_seconds_for(test_name: str) -> int:
    return 0 if test_name in CALENDAR_MONTHS else 60


@pytest.fixture
def env(sim_lifecycle_env: Env) -> Env:
    return sim_lifecycle_env


for _name, _test in vars(lifecycle).items():
    if _name.startswith("test_") and _name not in NOT_APPLICABLE:
        globals()[_name] = _test
