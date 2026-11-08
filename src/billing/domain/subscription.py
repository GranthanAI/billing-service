"""Subscription status and the only transition function. Pure: returns state plus effects."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from billing.domain.calendar import require_aware
from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.proration import unused_credit


class SubStatus(StrEnum):
    INCOMPLETE = "incomplete"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLING = "cancelling"
    ENDED = "ended"


GRANTS_PLAN = frozenset({SubStatus.ACTIVE, SubStatus.PAST_DUE, SubStatus.CANCELLING})


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    status: SubStatus
    plan_version_id: UUID
    interval: Interval
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    grace_until: datetime | None = None
    pending_plan_version_id: UUID | None = None
    pending_interval: Interval | None = None
    gateway_subscription_id: str | None = None
    next_gateway_subscription_id: str | None = None
    last_period_paid: Paise | None = None


# Triggers
@dataclass(frozen=True, slots=True)
class ChargeCaptured:
    period_start: datetime
    period_end: datetime
    amount: Paise


@dataclass(frozen=True, slots=True)
class AuthorisationTimedOut:
    pass


@dataclass(frozen=True, slots=True)
class ChargeFailed:
    pass


@dataclass(frozen=True, slots=True)
class GraceExpired:
    pass


@dataclass(frozen=True, slots=True)
class GatewayHalted:
    pass


@dataclass(frozen=True, slots=True)
class UserCancelled:
    pass


@dataclass(frozen=True, slots=True)
class CancelledExternally:
    pass


@dataclass(frozen=True, slots=True)
class PeriodEnded:
    pass


@dataclass(frozen=True, slots=True)
class UpgradeCharged:
    new_plan_version_id: UUID
    new_gateway_subscription_id: str
    period_start: datetime
    period_end: datetime
    amount: Paise
    new_interval: Interval | None = None  # set when the upgrade also changes the interval


@dataclass(frozen=True, slots=True)
class DowngradeScheduled:
    pending_plan_version_id: UUID
    next_gateway_subscription_id: str
    pending_interval: Interval | None = None


@dataclass(frozen=True, slots=True)
class NextMandateCharged:
    period_start: datetime
    period_end: datetime
    amount: Paise


@dataclass(frozen=True, slots=True)
class AccountDeleted:
    """The user deleted their account: end whatever they have and stop every mandate."""


@dataclass(frozen=True, slots=True)
class NextMandateFailed:
    """The first charge of the next mandate (a downgrade or a resume) failed."""


@dataclass(frozen=True, slots=True)
class Resumed:
    next_gateway_subscription_id: str


Trigger = (
    ChargeCaptured
    | AuthorisationTimedOut
    | ChargeFailed
    | GraceExpired
    | GatewayHalted
    | UserCancelled
    | CancelledExternally
    | PeriodEnded
    | UpgradeCharged
    | DowngradeScheduled
    | NextMandateCharged
    | NextMandateFailed
    | AccountDeleted
    | Resumed
)


# Effects
@dataclass(frozen=True, slots=True)
class EmitSubscriptionChanged:
    pass


@dataclass(frozen=True, slots=True)
class EmitPaymentFailed:
    pass


@dataclass(frozen=True, slots=True)
class CancelMandate:
    gateway_subscription_id: str
    at_cycle_end: bool


@dataclass(frozen=True, slots=True)
class IssueRefund:
    amount: Paise
    reason: str


Effect = EmitSubscriptionChanged | EmitPaymentFailed | CancelMandate | IssueRefund


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: SubscriptionState
    effects: tuple[Effect, ...]


class IllegalTransition(Exception):
    def __init__(self, from_status: SubStatus, trigger_name: str) -> None:
        super().__init__(f"{trigger_name} is not allowed from {from_status.value}")
        self.from_status = from_status
        self.trigger_name = trigger_name


_CHANGED = EmitSubscriptionChanged()


def _cancel(state: SubscriptionState, at_cycle_end: bool) -> tuple[Effect, ...]:
    if state.gateway_subscription_id is None:
        return ()
    return (CancelMandate(state.gateway_subscription_id, at_cycle_end),)


def _paid(state: SubscriptionState, t: ChargeCaptured | NextMandateCharged) -> SubscriptionState:
    return replace(
        state,
        current_period_start=t.period_start,
        current_period_end=t.period_end,
        last_period_paid=t.amount,
    )


def transition(
    state: SubscriptionState,
    trigger: Trigger,
    now: datetime,
    *,
    grace_days: int = 3,
    grace_seconds: int | None = None,  # when set, overrides grace_days (simulation runs)
) -> TransitionResult:
    require_aware(now)
    grace = (
        timedelta(days=grace_days) if grace_seconds is None else timedelta(seconds=grace_seconds)
    )
    s = state.status
    name = type(trigger).__name__

    def illegal() -> IllegalTransition:
        return IllegalTransition(s, name)

    match trigger:
        case ChargeCaptured():
            if s is SubStatus.INCOMPLETE:
                return TransitionResult(
                    replace(_paid(state, trigger), status=SubStatus.ACTIVE), (_CHANGED,)
                )
            if s is SubStatus.ACTIVE:
                return TransitionResult(_paid(state, trigger), ())
            if s in (SubStatus.PAST_DUE, SubStatus.SUSPENDED):
                new = replace(_paid(state, trigger), status=SubStatus.ACTIVE, grace_until=None)
                return TransitionResult(new, (_CHANGED,))
        case AuthorisationTimedOut():
            if s is SubStatus.INCOMPLETE:
                new = replace(state, status=SubStatus.ENDED)
                return TransitionResult(new, _cancel(state, False))
        case ChargeFailed():
            if s is SubStatus.ACTIVE:
                new = replace(
                    state,
                    status=SubStatus.PAST_DUE,
                    grace_until=now + grace,
                )
                return TransitionResult(new, (EmitPaymentFailed(),))
        case GraceExpired():
            if (
                s is SubStatus.PAST_DUE
                and state.grace_until is not None
                and now >= state.grace_until
            ):
                return TransitionResult(replace(state, status=SubStatus.SUSPENDED), (_CHANGED,))
        case GatewayHalted():
            if s in (SubStatus.PAST_DUE, SubStatus.SUSPENDED):
                new = replace(state, status=SubStatus.ENDED, grace_until=None)
                return TransitionResult(new, (*_cancel(state, False), _CHANGED))
        case UserCancelled():
            if s in (SubStatus.ACTIVE, SubStatus.PAST_DUE):
                new = replace(state, status=SubStatus.CANCELLING, grace_until=None)
                return TransitionResult(new, (*_cancel(state, True), _CHANGED))
        case CancelledExternally():
            if s in (SubStatus.ACTIVE, SubStatus.PAST_DUE):
                new = replace(state, status=SubStatus.CANCELLING, grace_until=None)
                return TransitionResult(new, (_CHANGED,))
            if s is SubStatus.SUSPENDED:
                new = replace(state, status=SubStatus.ENDED, grace_until=None)
                return TransitionResult(new, (_CHANGED,))
        case PeriodEnded():
            if (
                s is SubStatus.CANCELLING
                and state.current_period_end is not None
                and now >= state.current_period_end
            ):
                return TransitionResult(replace(state, status=SubStatus.ENDED), (_CHANGED,))
        case Resumed():
            if s is SubStatus.CANCELLING:
                new = replace(
                    state,
                    status=SubStatus.ACTIVE,
                    next_gateway_subscription_id=trigger.next_gateway_subscription_id,
                )
                return TransitionResult(new, (_CHANGED,))
        case UpgradeCharged():
            if s is SubStatus.ACTIVE:
                effects: list[Effect] = list(_cancel(state, False))
                # a downgrade mandate that was waiting would otherwise charge later
                if state.next_gateway_subscription_id is not None:
                    effects.append(CancelMandate(state.next_gateway_subscription_id, False))
                if (
                    state.last_period_paid is not None
                    and state.current_period_start is not None
                    and state.current_period_end is not None
                ):
                    refund = unused_credit(
                        state.last_period_paid,
                        state.current_period_start,
                        state.current_period_end,
                        now,
                    )
                    if refund > 0:
                        effects.append(IssueRefund(refund, "upgrade"))
                effects.append(_CHANGED)
                new = replace(
                    state,
                    plan_version_id=trigger.new_plan_version_id,
                    interval=trigger.new_interval or state.interval,
                    gateway_subscription_id=trigger.new_gateway_subscription_id,
                    next_gateway_subscription_id=None,
                    pending_plan_version_id=None,
                    pending_interval=None,
                    current_period_start=trigger.period_start,
                    current_period_end=trigger.period_end,
                    last_period_paid=trigger.amount,
                )
                return TransitionResult(new, tuple(effects))
        case DowngradeScheduled():
            if s is SubStatus.ACTIVE:
                new = replace(
                    state,
                    pending_plan_version_id=trigger.pending_plan_version_id,
                    pending_interval=trigger.pending_interval,
                    next_gateway_subscription_id=trigger.next_gateway_subscription_id,
                )
                return TransitionResult(new, _cancel(state, True))
        case AccountDeleted():
            if s is not SubStatus.ENDED:
                effects = list(_cancel(state, False))
                if state.next_gateway_subscription_id is not None:
                    effects.append(CancelMandate(state.next_gateway_subscription_id, False))
                effects.append(_CHANGED)
                new = replace(
                    state,
                    status=SubStatus.ENDED,
                    grace_until=None,
                    next_gateway_subscription_id=None,
                    pending_plan_version_id=None,
                    pending_interval=None,
                )
                return TransitionResult(new, tuple(effects))
        case NextMandateFailed():
            if s is SubStatus.ACTIVE and state.next_gateway_subscription_id is not None:
                # the period ended and the next mandate is the contract now, already failing
                new = replace(
                    state,
                    status=SubStatus.PAST_DUE,
                    plan_version_id=state.pending_plan_version_id or state.plan_version_id,
                    interval=state.pending_interval or state.interval,
                    gateway_subscription_id=state.next_gateway_subscription_id,
                    next_gateway_subscription_id=None,
                    pending_plan_version_id=None,
                    pending_interval=None,
                    grace_until=now + grace,
                )
                return TransitionResult(new, (EmitPaymentFailed(), _CHANGED))
        case NextMandateCharged():
            if s in (SubStatus.ACTIVE, SubStatus.CANCELLING) and (
                state.next_gateway_subscription_id is not None
            ):
                new = replace(
                    _paid(state, trigger),
                    status=SubStatus.ACTIVE,
                    plan_version_id=state.pending_plan_version_id or state.plan_version_id,
                    interval=state.pending_interval or state.interval,
                    gateway_subscription_id=state.next_gateway_subscription_id,
                    next_gateway_subscription_id=None,
                    pending_plan_version_id=None,
                    pending_interval=None,
                )
                return TransitionResult(new, (_CHANGED,))
    raise illegal()


def map_gateway_status(
    gw_status: str,
    *,
    is_next_mandate: bool,
    now: datetime,
    current_period_end: datetime | None,
    grace_until: datetime | None,
) -> SubStatus | None:
    """Razorpay status to ours; None means no change."""
    require_aware(now)
    match gw_status:
        case "created" | "authenticated":
            return None if is_next_mandate else SubStatus.INCOMPLETE
        case "active":
            return SubStatus.ACTIVE
        case "pending":
            if grace_until is not None and now >= grace_until:
                return SubStatus.SUSPENDED
            return SubStatus.PAST_DUE
        case "halted" | "completed" | "expired":
            return SubStatus.ENDED
        case "cancelled":
            if current_period_end is not None and now < current_period_end:
                return SubStatus.CANCELLING
            return SubStatus.ENDED
        case "paused":
            return SubStatus.SUSPENDED
    raise ValueError(f"unknown gateway status: {gw_status}")
