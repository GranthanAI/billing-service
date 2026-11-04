"""Gateway events -> triggers. `derive` is pure; `EventApplier` is the one path the webhook
processor, `checkout/verify` and `checkout.reconcile` all go through."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import structlog

from billing.app.charges import record_charge
from billing.app.config import BillingContext
from billing.app.effects import EffectContext
from billing.app.ports import GatewayPayment, GatewayRefund, GatewaySubscription, UnitOfWork
from billing.app.records import ChangeKind, ChangeRecord, ChangeStatus
from billing.app.transitions import IllegalTransition, apply_transition
from billing.domain.catalog import Interval
from billing.domain.subscription import (
    CancelledExternally,
    ChargeCaptured,
    ChargeFailed,
    DowngradeScheduled,
    GatewayHalted,
    NextMandateCharged,
    NextMandateFailed,
    Resumed,
    SubscriptionState,
    SubStatus,
    Trigger,
    UpgradeCharged,
)
from billing.observability import (
    REFUND_FAILED,
    WEBHOOK_UNEXPECTED,
)

log = structlog.get_logger()

PROCESSED = "processed"
IGNORED = "ignored"


class Role(StrEnum):
    CURRENT = "current"
    NEXT = "next"
    CHANGE = "change"


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    """An event with the Razorpay entities already fetched (outside any transaction)."""

    event_id: str
    type: str
    subscription: GatewaySubscription | None = None  # fetched fresh
    snapshot: GatewaySubscription | None = None  # as embedded in the webhook body
    payment: GatewayPayment | None = None
    refund: GatewayRefund | None = None


@dataclass(frozen=True, slots=True)
class Derivation:
    triggers: tuple[Trigger, ...] = ()
    change_status: ChangeStatus | None = None  # applies once the triggers did
    unexpected: bool = False
    # `subscription.charged` only: what the payment, invoice and ledger are for
    charge_plan_version_id: UUID | None = None
    charge_interval: Interval | None = None
    charge_purpose: str = "subscription"
    period: tuple[datetime, datetime] | None = None
    reasons: tuple[str, ...] = field(default=(), compare=False)


_FAILED_OK = {"subscription.pending": {"pending", "halted", "paused"}}


def role_of(state: SubscriptionState, mandate_id: str, change: ChangeRecord | None) -> Role | None:
    if state.gateway_subscription_id == mandate_id:
        return Role.CURRENT
    if state.next_gateway_subscription_id == mandate_id:
        return Role.NEXT
    if change is not None and change.gateway_subscription_id == mandate_id:
        return Role.CHANGE
    return None


def _authorise(change: ChangeRecord) -> Trigger | None:
    if change.kind is ChangeKind.DOWNGRADE:
        return DowngradeScheduled(
            change.new_plan_version_id, change.gateway_subscription_id, change.new_interval
        )
    if change.kind is ChangeKind.RESUME:
        return Resumed(change.gateway_subscription_id)
    return None


def derive(
    event: GatewayEvent,
    state: SubscriptionState,
    role: Role,
    change: ChangeRecord | None,
) -> Derivation:
    """The trigger table of the LLD, one branch per row."""
    kind = event.type
    fresh_status = event.subscription.status if event.subscription else None

    if kind == "subscription.authenticated":
        if (
            role is Role.CHANGE
            and change is not None
            and change.status is ChangeStatus.PENDING
            and (trig := _authorise(change)) is not None
        ):
            return Derivation((trig,), ChangeStatus.AUTHORISED)
        return Derivation()
    if kind == "subscription.activated":
        return Derivation()

    if kind == "subscription.charged":
        return _derive_charged(event, state, role, change)

    if kind in ("subscription.pending", "subscription.paused"):
        if role not in (Role.CURRENT, Role.NEXT):
            return Derivation()
        allowed = _FAILED_OK.get(kind, {"paused"})
        if fresh_status not in allowed:  # a later event already moved Razorpay on
            return Derivation(reasons=("stale",))
        if role is Role.NEXT:  # the first charge of a downgrade or resume mandate failed
            return Derivation((NextMandateFailed(),))
        return Derivation((ChargeFailed(),))

    if kind == "subscription.halted":
        if fresh_status != "halted":
            return Derivation()
        if role is Role.CURRENT:
            if state.status is SubStatus.ACTIVE:  # the `pending` event never reached us
                return Derivation((ChargeFailed(), GatewayHalted()))
            return Derivation((GatewayHalted(),))
        if role is Role.NEXT:  # the next mandate took over, failed, and Razorpay gave up
            return Derivation((NextMandateFailed(), GatewayHalted()))
        return Derivation()

    if kind == "subscription.cancelled":
        if role is not Role.CURRENT or fresh_status != "cancelled":
            return Derivation()
        if state.next_gateway_subscription_id is not None:
            return Derivation(reasons=("mandate swap",))  # the old mandate ending is the swap
        return Derivation((CancelledExternally(),))

    if kind in ("subscription.completed", "subscription.expired"):
        return Derivation(unexpected=role is Role.CURRENT)

    return Derivation()


def _derive_charged(
    event: GatewayEvent, state: SubscriptionState, role: Role, change: ChangeRecord | None
) -> Derivation:
    if event.payment is None:
        raise ValueError("subscription.charged without a payment")
    src = event.snapshot or event.subscription
    if src is None or src.current_start is None or src.current_end is None:
        raise ValueError("subscription.charged without a billing period")
    start, end = src.current_start, src.current_end
    amount = event.payment.amount

    if role is Role.CURRENT:
        return Derivation(
            (ChargeCaptured(start, end, amount),),
            charge_plan_version_id=state.plan_version_id,
            charge_interval=state.interval,
            period=(start, end),
        )
    if role is Role.NEXT:
        return Derivation(
            (NextMandateCharged(start, end, amount),),
            ChangeStatus.APPLIED if change is not None else None,
            charge_plan_version_id=state.pending_plan_version_id or state.plan_version_id,
            charge_interval=state.pending_interval or state.interval,
            period=(start, end),
        )
    assert change is not None
    plan = dict(
        charge_plan_version_id=change.new_plan_version_id,
        charge_interval=change.new_interval,
        period=(start, end),
    )
    if change.status in (ChangeStatus.APPLIED, ChangeStatus.ABANDONED):
        # money arrived on a mandate we no longer track: record it, change nothing
        return Derivation(reasons=("dead change",), **plan)  # type: ignore[arg-type]
    if change.kind is ChangeKind.UPGRADE:
        return Derivation(
            (
                UpgradeCharged(
                    change.new_plan_version_id,
                    change.gateway_subscription_id,
                    start,
                    end,
                    amount,
                    change.new_interval,
                ),
            ),
            ChangeStatus.APPLIED,
            charge_purpose="upgrade",
            **plan,  # type: ignore[arg-type]
        )
    # downgrade / resume whose `authenticated` event has not been seen yet: do both steps
    auth = _authorise(change)
    assert auth is not None
    triggers: tuple[Trigger, ...] = (auth, NextMandateCharged(start, end, amount))
    return Derivation(triggers, ChangeStatus.APPLIED, **plan)  # type: ignore[arg-type]


class EventApplier:
    def __init__(self, bctx: BillingContext) -> None:
        self._b = bctx

    async def apply_gateway_event(self, event: GatewayEvent) -> str:
        """Own transaction. Used by checkout/verify and checkout.reconcile."""
        async with self._b.uow_factory() as uow:
            return await self.apply(uow, event)

    async def apply(self, uow: UnitOfWork, event: GatewayEvent) -> str:
        """Inside the caller's transaction. Returns `processed` or `ignored`."""
        if event.type.startswith("refund."):
            return await self._apply_refund(uow, event)
        if event.subscription is None:
            return IGNORED
        mandate = event.subscription.id
        change = await uow.changes.by_gateway_id(mandate)
        sub = await uow.subscriptions.lock_by_gateway_id(mandate)
        if sub is None and change is not None:
            sub = await uow.subscriptions.lock(change.subscription_id)
        if sub is None:
            log.info("webhook_unknown_mandate", mandate=mandate, gateway_event=event.type)
            return IGNORED
        role = role_of(sub.state, mandate, change)
        if role is None:
            return IGNORED

        d = derive(event, sub.state, role, change)
        if d.unexpected:
            WEBHOOK_UNEXPECTED.labels(event_type=event.type).inc()
            log.error("webhook_unexpected", gateway_event=event.type, subscription_id=str(sub.id))
            return IGNORED

        effect_ctx = EffectContext(
            failed_payment=event.payment,
            next_retry_at=event.subscription.charge_at,
            refund_payment_id=change.refund_payment_id if change else None,
            refund_key=f"upgrade:{change.id}" if change else None,
        )
        if event.type == "subscription.charged":
            assert event.payment is not None and d.period is not None
            assert d.charge_plan_version_id is not None and d.charge_interval is not None
            plan = await uow.plans.by_id(d.charge_plan_version_id)
            assert plan is not None
            recorded = await record_charge(
                uow,
                sub,
                event.payment,
                d.period[0],
                d.period[1],
                d.charge_interval,
                sub.seats,
                plan_code=plan.plan.plan_code,
                config=self._b.config,
                ids=self._b.ids,
                purpose=d.charge_purpose,
            )
            if not recorded:  # applied before (verify, reconcile or a redelivery)
                return PROCESSED

        if not d.triggers:
            return IGNORED
        applied = False
        for trigger in d.triggers:
            try:
                sub = await apply_transition(
                    uow, self._b, sub, trigger, cause_ref=event.event_id, ctx=effect_ctx
                )
                applied = True
            except IllegalTransition as exc:
                log.info(
                    "stale_webhook_ignored",
                    gateway_event=event.type,
                    status=sub.state.status.value,
                    trigger=exc.trigger_name,
                )
                if event.type == "subscription.charged":
                    log.warning(
                        "charge_without_transition",
                        subscription_id=str(sub.id),
                        payment_id=event.payment.id if event.payment else None,
                    )
                break
        if applied and change is not None and d.change_status is not None:
            await uow.changes.set_status(change.id, d.change_status)
        return PROCESSED

    async def _apply_refund(self, uow: UnitOfWork, event: GatewayEvent) -> str:
        refund = event.refund
        if refund is None:
            return IGNORED
        status = "failed" if event.type == "refund.failed" else refund.status
        if not await uow.refunds.set_status(refund.id, status):
            if await uow.payments.by_gateway_id(refund.payment_id) is None:
                return IGNORED  # not one of ours
            raise LookupError(f"refund {refund.id} is not recorded yet")  # retry shortly
        if event.type == "refund.failed":
            REFUND_FAILED.inc()
            log.error("refund_failed", refund_id=refund.id, payment_id=refund.payment_id)
        return PROCESSED
