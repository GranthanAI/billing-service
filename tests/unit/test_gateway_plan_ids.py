"""G1: which stored gateway plan id may be kept, which must be recreated, and the checkout guard (pure logic, no database)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from billing.app.catalog import PlanIdAction, decide_plan_id, kind_of_plan_id
from billing.app.errors import PlanGatewayMismatch, ValidationFailed
from billing.app.subscriptions import _plan_id
from billing.app.records import PlanVersionRecord
from billing.domain.catalog import Interval
from tests.support import plans


@pytest.mark.parametrize(
    "plan_id,kind",
    [("plan_fake_0001", "fake"), ("plan_SIMabc123XYZ789", "simulator"), ("plan_Nk3j2h1g0f9e8d", "razorpay"), ("sub_x", None), ("", None), (None, None)],
)
def test_kind_from_prefix(plan_id, kind):
    assert kind_of_plan_id(plan_id) == kind


@pytest.mark.parametrize(
    "stored,stored_kind,current,expected",
    [
        (None, None, "simulator", PlanIdAction.MISSING),
        ("", "simulator", "simulator", PlanIdAction.MISSING),
        ("plan_fake_0001", "fake", "simulator", PlanIdAction.FOREIGN),
        ("plan_fake_0001", None, "simulator", PlanIdAction.FOREIGN),          # kind not recorded: guessed from the prefix
        ("plan_SIMabc123XYZ789", "simulator", "razorpay", PlanIdAction.FOREIGN),
        ("plan_Nk3j2h1g0f9e8d", "razorpay", "simulator", PlanIdAction.FOREIGN),
        ("plan_SIMabc123XYZ789", "simulator", "simulator", PlanIdAction.KEEP),
        ("plan_SIMabc123XYZ789", None, "simulator", PlanIdAction.KEEP),
        ("plan_Nk3j2h1g0f9e8d", "razorpay", "razorpay", PlanIdAction.KEEP),
        ("plan_fake_0001", "fake", "fake", PlanIdAction.KEEP),
    ],
)
def test_decision(stored, stored_kind, current, expected):
    assert decide_plan_id(stored, stored_kind, current) is expected


def test_checkout_refuses_a_foreign_plan_id_instead_of_calling_the_gateway():
    rec = PlanVersionRecord(plan=plans.PRO, effective_from=datetime(2026, 11, 1, tzinfo=UTC), checksum="c")
    mine = replace(rec, razorpay_plan_month="plan_SIMabc123XYZ789", razorpay_plan_year="plan_SIMdef456UVW012", gateway_kind="simulator")
    assert _plan_id(mine, Interval.MONTH, "simulator") == "plan_SIMabc123XYZ789"
    foreign = replace(mine, razorpay_plan_month="plan_fake_0001", gateway_kind="fake")
    with pytest.raises(PlanGatewayMismatch) as e:
        _plan_id(foreign, Interval.MONTH, "simulator")
    assert "catalog sync" in e.value.detail and "pro" in e.value.detail
    with pytest.raises(PlanGatewayMismatch):                                        # kind not stored: judged by the prefix
        _plan_id(replace(foreign, gateway_kind=None), Interval.MONTH, "razorpay")
    assert _plan_id(foreign, Interval.MONTH) == "plan_fake_0001"                    # no gateway given: unchanged behaviour
    with pytest.raises(ValidationFailed):
        _plan_id(replace(mine, razorpay_plan_month=None), Interval.MONTH, "simulator")
