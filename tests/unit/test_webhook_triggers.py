"""Trigger derivation: one test per row of the LLD table (plus the guards around it)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from billing.app.ports import GatewayPayment, GatewaySubscription
from billing.app.records import ChangeKind, ChangeRecord, ChangeStatus
from billing.app.webhooks import GatewayEvent, Role, derive, role_of
from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.subscription import (
    CancelledExternally,
    ChargeCaptured,
    ChargeFailed,
    DowngradeScheduled,
    GatewayHalted,
    NextMandateCharged,
    Resumed,
    SubscriptionState,
    SubStatus,
    UpgradeCharged,
)

START = datetime(2026, 11, 1, 4, 45, tzinfo=UTC)
END = datetime(2026, 12, 1, 4, 45, tzinfo=UTC)
PRO, MAX = UUID(int=2), UUID(int=3)


def sub(status: str = "active", sid: str = "sub_cur") -> GatewaySubscription:
    return GatewaySubscription(sid, "plan_1", status, START, END, END, START, 1, {})


def pay(amount: int = 49900) -> GatewayPayment:
    return GatewayPayment(
        "pay_1", Paise(amount), "INR", "captured", "card", Paise(1000), Paise(180),
        None, None, None, START, {}, {},
    )  # fmt: skip


def event(
    kind: str, status: str = "active", sid: str = "sub_cur", payment: bool = False
) -> GatewayEvent:
    s = sub(status, sid)
    return GatewayEvent("evt", kind, s, s, pay() if payment else None)


def state(**kw: object) -> SubscriptionState:
    base: dict[str, object] = dict(
        status=SubStatus.ACTIVE,
        plan_version_id=PRO,
        interval=Interval.MONTH,
        gateway_subscription_id="sub_cur",
        current_period_start=START,
        current_period_end=END,
    )
    base.update(kw)
    return SubscriptionState(**base)  # type: ignore[arg-type]


def change(kind: ChangeKind, status: ChangeStatus = ChangeStatus.PENDING) -> ChangeRecord:
    return ChangeRecord(
        UUID(int=9), UUID(int=8), kind, MAX, Interval.MONTH, "sub_chg", status, UUID(int=7)
    )


def run(
    kind: str,
    st: SubscriptionState,
    mandate: str = "sub_cur",
    ch: ChangeRecord | None = None,
    **ev: object,
):  # type: ignore[no-untyped-def]
    role = role_of(st, mandate, ch)
    assert role is not None
    status = str(ev.pop("status", "active"))
    return derive(event(kind, status, mandate, bool(ev.get("payment"))), st, role, ch)


def test_roles() -> None:
    st = state(next_gateway_subscription_id="sub_next")
    assert role_of(st, "sub_cur", None) is Role.CURRENT
    assert role_of(st, "sub_next", None) is Role.NEXT
    assert role_of(st, "sub_chg", change(ChangeKind.UPGRADE)) is Role.CHANGE
    assert role_of(st, "sub_zzz", None) is None


# authenticated
def test_authenticated_downgrade_row() -> None:
    d = run(
        "subscription.authenticated",
        state(),
        "sub_chg",
        change(ChangeKind.DOWNGRADE),
        status="authenticated",
    )
    assert d.triggers == (DowngradeScheduled(MAX, "sub_chg", Interval.MONTH),)
    assert d.change_status is ChangeStatus.AUTHORISED


def test_authenticated_resume_row() -> None:
    d = run(
        "subscription.authenticated",
        state(status=SubStatus.CANCELLING),
        "sub_chg",
        change(ChangeKind.RESUME),
        status="authenticated",
    )
    assert d.triggers == (Resumed("sub_chg"),)
    assert d.change_status is ChangeStatus.AUTHORISED


@pytest.mark.parametrize("kind", ["subscription.authenticated", "subscription.activated"])
def test_authenticated_and_activated_otherwise_do_nothing(kind: str) -> None:
    assert run(kind, state()).triggers == ()
    upgrade = change(ChangeKind.UPGRADE)
    assert run(kind, state(), "sub_chg", upgrade).triggers == ()
    applied = change(ChangeKind.DOWNGRADE, ChangeStatus.APPLIED)  # a late duplicate
    assert run(kind, state(), "sub_chg", applied).triggers == ()


# charged
def test_charged_current_mandate() -> None:
    d = run("subscription.charged", state(), payment=True)
    assert d.triggers == (ChargeCaptured(START, END, Paise(49900)),)
    assert (d.charge_plan_version_id, d.charge_interval, d.charge_purpose) == (
        PRO,
        Interval.MONTH,
        "subscription",
    )
    assert d.period == (START, END)


def test_charged_next_mandate_applies_the_pending_plan() -> None:
    st = state(
        next_gateway_subscription_id="sub_next",
        pending_plan_version_id=MAX,
        pending_interval=Interval.YEAR,
    )
    applied = change(ChangeKind.DOWNGRADE, ChangeStatus.AUTHORISED)
    d = run("subscription.charged", st, "sub_next", applied, payment=True)
    assert d.triggers == (NextMandateCharged(START, END, Paise(49900)),)
    assert d.change_status is ChangeStatus.APPLIED
    assert (d.charge_plan_version_id, d.charge_interval) == (MAX, Interval.YEAR)


def test_charged_next_mandate_without_a_change_row() -> None:
    d = run(
        "subscription.charged",
        state(next_gateway_subscription_id="sub_next"),
        "sub_next",
        None,
        payment=True,
    )
    assert isinstance(d.triggers[0], NextMandateCharged) and d.change_status is None


def test_charged_upgrade_row() -> None:
    d = run("subscription.charged", state(), "sub_chg", change(ChangeKind.UPGRADE), payment=True)
    assert d.triggers == (UpgradeCharged(MAX, "sub_chg", START, END, Paise(49900), Interval.MONTH),)
    assert d.change_status is ChangeStatus.APPLIED
    assert (d.charge_plan_version_id, d.charge_purpose) == (MAX, "upgrade")


def test_charged_downgrade_whose_authentication_was_missed_does_both_steps() -> None:
    d = run("subscription.charged", state(), "sub_chg", change(ChangeKind.DOWNGRADE), payment=True)
    assert d.triggers == (
        DowngradeScheduled(MAX, "sub_chg", Interval.MONTH),
        NextMandateCharged(START, END, Paise(49900)),
    )


def test_charged_on_an_abandoned_change_is_recorded_but_changes_nothing() -> None:
    d = run(
        "subscription.charged",
        state(),
        "sub_chg",
        change(ChangeKind.UPGRADE, ChangeStatus.ABANDONED),
        payment=True,
    )
    assert d.triggers == () and d.period == (START, END)


def test_charged_period_comes_from_the_webhook_snapshot() -> None:
    later = GatewaySubscription(
        "sub_cur", "plan_1", "active", END, datetime(2027, 1, 1, tzinfo=UTC), None, None, 2, {}
    )
    ev = GatewayEvent("e", "subscription.charged", later, sub(), pay())
    d = derive(ev, state(), Role.CURRENT, None)
    assert d.period == (START, END)  # not the newer period Razorpay has moved on to


def test_charged_without_a_payment_is_an_error_to_retry() -> None:
    with pytest.raises(ValueError):
        derive(event("subscription.charged"), state(), Role.CURRENT, None)


# failures
@pytest.mark.parametrize(
    "kind,fresh", [("subscription.pending", "pending"), ("subscription.paused", "paused")]
)
def test_pending_and_paused_on_the_current_mandate_fail_the_charge(kind: str, fresh: str) -> None:
    assert run(kind, state(), status=fresh).triggers == (ChargeFailed(),)


@pytest.mark.parametrize("kind", ["subscription.pending", "subscription.paused"])
def test_failure_events_for_other_mandates_are_ignored(kind: str) -> None:
    st = state(next_gateway_subscription_id="sub_next")
    assert run(kind, st, "sub_next", status="pending").triggers == ()


def test_a_failure_event_made_stale_by_a_recovery_is_ignored() -> None:
    d = run("subscription.pending", state(), status="active")
    assert d.triggers == () and d.reasons == ("stale",)


def test_halted_current_mandate() -> None:
    assert run("subscription.halted", state(), status="halted").triggers == (GatewayHalted(),)
    assert run("subscription.halted", state(), status="active").triggers == ()


def test_cancelled_current_mandate_is_an_external_cancel() -> None:
    assert run("subscription.cancelled", state(), status="cancelled").triggers == (
        CancelledExternally(),
    )


def test_cancelled_old_mandate_during_a_swap_is_ignored() -> None:
    d = run(
        "subscription.cancelled", state(next_gateway_subscription_id="sub_next"), status="cancelled"
    )
    assert d.triggers == () and d.reasons == ("mandate swap",)


def test_cancelled_other_mandates_are_ignored() -> None:
    st = state(next_gateway_subscription_id="sub_next")
    assert run("subscription.cancelled", st, "sub_next", status="cancelled").triggers == ()
    assert (
        run(
            "subscription.cancelled",
            state(),
            "sub_chg",
            change(ChangeKind.UPGRADE),
            status="cancelled",
        ).triggers
        == ()
    )


@pytest.mark.parametrize("kind", ["subscription.completed", "subscription.expired"])
def test_completed_and_expired_raise_the_alert_flag(kind: str) -> None:
    d = run(kind, state(), status=kind.split(".")[1])
    assert d.triggers == () and d.unexpected is True
    other = run(kind, state(next_gateway_subscription_id="n"), "n", status=kind.split(".")[1])
    assert other.unexpected is False  # only the current mandate is a surprise


@pytest.mark.parametrize(
    "kind", ["payment.captured", "payment.failed", "subscription.updated", "order.paid"]
)
def test_everything_else_is_ignored(kind: str) -> None:
    d = derive(GatewayEvent("e", kind, sub()), state(), Role.CURRENT, None)
    assert d.triggers == () and not d.unexpected
