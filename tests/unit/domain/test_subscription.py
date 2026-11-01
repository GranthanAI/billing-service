from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.subscription import (
    AuthorisationTimedOut,
    CancelledExternally,
    CancelMandate,
    ChargeCaptured,
    ChargeFailed,
    DowngradeScheduled,
    EmitPaymentFailed,
    EmitSubscriptionChanged,
    GatewayHalted,
    GraceExpired,
    IllegalTransition,
    IssueRefund,
    NextMandateCharged,
    PeriodEnded,
    Resumed,
    SubscriptionState,
    SubStatus,
    UpgradeCharged,
    UserCancelled,
    map_gateway_status,
    transition,
)

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
DAY30 = timedelta(days=30)
PRO, MAX, FREE_ID = UUID(int=2), UUID(int=3), UUID(int=1)
CH = EmitSubscriptionChanged()
GRACE_STATUSES = (SubStatus.PAST_DUE, SubStatus.SUSPENDED)


def st(
    status: SubStatus,
    *,
    next_id: str | None = None,
    pending: UUID | None = None,
    no_period: bool = False,
) -> SubscriptionState:
    return SubscriptionState(
        status=status,
        plan_version_id=PRO,
        interval=Interval.MONTH,
        current_period_start=None if no_period else T0,
        current_period_end=None if no_period else T0 + DAY30,
        grace_until=T0 + timedelta(days=3) if status in GRACE_STATUSES else None,
        pending_plan_version_id=pending,
        gateway_subscription_id="sub_1",
        next_gateway_subscription_id=next_id,
        last_period_paid=Paise(49900),
    )


def charge(at: datetime = T0, amount: int = 49900) -> ChargeCaptured:
    return ChargeCaptured(at, at + DAY30, Paise(amount))


def test_incomplete_charge_activates() -> None:
    r = transition(st(SubStatus.INCOMPLETE, no_period=True), charge(), T0)
    assert r.state.status is SubStatus.ACTIVE
    assert r.state.current_period_end == T0 + DAY30
    assert r.state.last_period_paid == 49900
    assert r.effects == (CH,)


def test_incomplete_timeout_ends() -> None:
    r = transition(st(SubStatus.INCOMPLETE), AuthorisationTimedOut(), T0)
    assert r.state.status is SubStatus.ENDED
    assert r.effects == (CancelMandate("sub_1", False),)


def test_active_renewal_advances_period() -> None:
    nxt = T0 + DAY30
    r = transition(st(SubStatus.ACTIVE), charge(nxt), nxt)
    assert r.state.status is SubStatus.ACTIVE
    assert r.state.current_period_start == nxt
    assert r.effects == ()


def test_active_failed_goes_past_due_with_grace() -> None:
    r = transition(st(SubStatus.ACTIVE), ChargeFailed(), T0)
    assert r.state.status is SubStatus.PAST_DUE
    assert r.state.grace_until == T0 + timedelta(days=3)
    assert r.effects == (EmitPaymentFailed(),)
    r2 = transition(st(SubStatus.ACTIVE), ChargeFailed(), T0, grace_days=5)
    assert r2.state.grace_until == T0 + timedelta(days=5)


@pytest.mark.parametrize("status", GRACE_STATUSES)
def test_late_charge_restores(status: SubStatus) -> None:
    r = transition(st(status), charge(), T0)
    assert r.state.status is SubStatus.ACTIVE
    assert r.state.grace_until is None
    assert r.effects == (CH,)


def test_grace_expired_guard() -> None:
    s = st(SubStatus.PAST_DUE)
    edge = T0 + timedelta(days=3)
    with pytest.raises(IllegalTransition):
        transition(s, GraceExpired(), edge - timedelta(seconds=1))
    r = transition(s, GraceExpired(), edge)
    assert r.state.status is SubStatus.SUSPENDED
    assert r.state.grace_until is not None
    assert r.effects == (CH,)


@pytest.mark.parametrize("status", GRACE_STATUSES)
def test_halted_ends(status: SubStatus) -> None:
    r = transition(st(status), GatewayHalted(), T0)
    assert r.state.status is SubStatus.ENDED
    assert r.state.grace_until is None
    assert r.effects == (CancelMandate("sub_1", False), CH)


@pytest.mark.parametrize("status", [SubStatus.ACTIVE, SubStatus.PAST_DUE])
def test_user_cancel(status: SubStatus) -> None:
    r = transition(st(status), UserCancelled(), T0)
    assert r.state.status is SubStatus.CANCELLING
    assert r.state.grace_until is None
    assert r.effects == (CancelMandate("sub_1", True), CH)


@pytest.mark.parametrize("status", [SubStatus.ACTIVE, SubStatus.PAST_DUE])
def test_external_cancel_keeps_access(status: SubStatus) -> None:
    r = transition(st(status), CancelledExternally(), T0)
    assert r.state.status is SubStatus.CANCELLING
    assert r.state.grace_until is None
    assert r.effects == (CH,)


def test_external_cancel_when_suspended_ends() -> None:
    r = transition(st(SubStatus.SUSPENDED), CancelledExternally(), T0)
    assert r.state.status is SubStatus.ENDED
    assert r.state.grace_until is None
    assert r.effects == (CH,)


def test_period_ended_guard() -> None:
    s = st(SubStatus.CANCELLING)
    end = T0 + DAY30
    with pytest.raises(IllegalTransition):
        transition(s, PeriodEnded(), end - timedelta(seconds=1))
    r = transition(s, PeriodEnded(), end)
    assert r.state.status is SubStatus.ENDED
    assert r.effects == (CH,)


def test_resume() -> None:
    r = transition(st(SubStatus.CANCELLING), Resumed("sub_2"), T0)
    assert r.state.status is SubStatus.ACTIVE
    assert r.state.next_gateway_subscription_id == "sub_2"
    assert r.effects == (CH,)


def test_upgrade_refund_example() -> None:
    now = T0 + timedelta(days=15)
    up = UpgradeCharged(MAX, "sub_2", now, now + DAY30, Paise(149900))
    r = transition(st(SubStatus.ACTIVE, pending=FREE_ID), up, now)
    assert r.state.plan_version_id == MAX
    assert r.state.gateway_subscription_id == "sub_2"
    assert r.state.current_period_start == now
    assert r.state.last_period_paid == 149900
    assert r.state.pending_plan_version_id is None
    assert r.effects == (CancelMandate("sub_1", False), IssueRefund(Paise(24950), "upgrade"), CH)


def test_upgrade_changes_interval_and_cancels_waiting_mandate() -> None:
    now = T0 + DAY30  # nothing unused, so no refund
    up = UpgradeCharged(
        PRO, "sub_3", now, now + timedelta(days=365), Paise(499000), new_interval=Interval.YEAR
    )
    r = transition(st(SubStatus.ACTIVE, next_id="sub_next", pending=FREE_ID), up, now)
    assert r.state.interval is Interval.YEAR
    assert r.state.next_gateway_subscription_id is None
    assert r.effects == (CancelMandate("sub_1", False), CancelMandate("sub_next", False), CH)


def test_downgrade_scheduled_then_applied() -> None:
    r = transition(
        st(SubStatus.ACTIVE), DowngradeScheduled(FREE_ID, "sub_next", Interval.MONTH), T0
    )
    assert r.state.pending_plan_version_id == FREE_ID
    assert r.state.plan_version_id == PRO  # nothing changes for the user yet
    assert r.effects == (CancelMandate("sub_1", True),)
    end = T0 + DAY30
    r2 = transition(r.state, NextMandateCharged(end, end + DAY30, Paise(1)), end)
    assert r2.state.plan_version_id == FREE_ID
    assert r2.state.gateway_subscription_id == "sub_next"
    assert r2.state.next_gateway_subscription_id is None
    assert r2.state.pending_plan_version_id is None
    assert r2.state.current_period_start == end
    assert r2.effects == (CH,)


def test_next_mandate_after_resume_keeps_plan() -> None:
    r = transition(
        st(SubStatus.CANCELLING, next_id="sub_2"),
        NextMandateCharged(T0, T0 + DAY30, Paise(49900)),
        T0,
    )
    assert r.state.status is SubStatus.ACTIVE
    assert r.state.plan_version_id == PRO
    assert r.state.gateway_subscription_id == "sub_2"


def test_next_mandate_needs_next_id() -> None:
    with pytest.raises(IllegalTransition):
        transition(st(SubStatus.ACTIVE), NextMandateCharged(T0, T0, Paise(1)), T0)


ALL_TRIGGERS = [
    charge(),
    AuthorisationTimedOut(),
    ChargeFailed(),
    GraceExpired(),
    GatewayHalted(),
    UserCancelled(),
    CancelledExternally(),
    PeriodEnded(),
    UpgradeCharged(MAX, "x", T0, T0, Paise(1)),
    DowngradeScheduled(FREE_ID, "x"),
    NextMandateCharged(T0, T0, Paise(1)),
    Resumed("x"),
]
LEGAL = {
    SubStatus.INCOMPLETE: {"ChargeCaptured", "AuthorisationTimedOut"},
    SubStatus.ACTIVE: {
        "ChargeCaptured",
        "ChargeFailed",
        "UserCancelled",
        "CancelledExternally",
        "UpgradeCharged",
        "DowngradeScheduled",
        "NextMandateCharged",
    },
    SubStatus.PAST_DUE: {
        "ChargeCaptured",
        "GraceExpired",
        "GatewayHalted",
        "UserCancelled",
        "CancelledExternally",
    },
    SubStatus.SUSPENDED: {"ChargeCaptured", "GatewayHalted", "CancelledExternally"},
    SubStatus.CANCELLING: {"PeriodEnded", "Resumed", "NextMandateCharged"},
    SubStatus.ENDED: set(),
}


@pytest.mark.parametrize("status", list(SubStatus))
def test_exactly_the_table_is_legal(status: SubStatus) -> None:
    # now is far in the future so the time guards pass; next_id lets NextMandateCharged pass
    s = st(status, next_id="n")
    ok: set[str] = set()
    for trig in ALL_TRIGGERS:
        name = type(trig).__name__
        try:
            transition(s, trig, T0 + timedelta(days=60))
            ok.add(name)
        except IllegalTransition as e:
            assert e.from_status is status
            assert e.trigger_name == name
    assert ok == LEGAL[status]


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError):
        transition(st(SubStatus.ACTIVE), ChargeFailed(), datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("gw", "nxt", "days", "expected"),
    [
        ("created", False, 0, SubStatus.INCOMPLETE),
        ("authenticated", False, 0, SubStatus.INCOMPLETE),
        ("created", True, 0, None),
        ("authenticated", True, 0, None),
        ("active", False, 0, SubStatus.ACTIVE),
        ("pending", False, 1, SubStatus.PAST_DUE),
        ("pending", False, 5, SubStatus.SUSPENDED),
        ("halted", False, 0, SubStatus.ENDED),
        ("cancelled", False, 1, SubStatus.CANCELLING),
        ("cancelled", False, 31, SubStatus.ENDED),
        ("completed", False, 0, SubStatus.ENDED),
        ("expired", False, 0, SubStatus.ENDED),
        ("paused", False, 0, SubStatus.SUSPENDED),
    ],
)
def test_gateway_status_mapping(gw: str, nxt: bool, days: int, expected: SubStatus | None) -> None:
    got = map_gateway_status(
        gw,
        is_next_mandate=nxt,
        now=T0 + timedelta(days=days),
        current_period_end=T0 + DAY30,
        grace_until=T0 + timedelta(days=3),
    )
    assert got == expected


def test_gateway_status_unknown_and_missing_dates() -> None:
    kw = {"is_next_mandate": False, "now": T0, "current_period_end": None, "grace_until": None}
    with pytest.raises(ValueError):
        map_gateway_status("weird", **kw)  # type: ignore[arg-type]
    assert map_gateway_status("pending", **kw) is SubStatus.PAST_DUE  # type: ignore[arg-type]
    assert map_gateway_status("cancelled", **kw) is SubStatus.ENDED  # type: ignore[arg-type]
