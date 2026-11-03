"""Runs the effects a domain transition asks for, inside the transition's own transaction."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from billing.app.events import PaymentFailedEvent, SubscriptionChangedEvent
from billing.app.ports import GatewayPayment, UnitOfWork
from billing.app.records import BillingAccount, SubscriptionRecord
from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.subscription import (
    CancelMandate,
    Effect,
    EmitPaymentFailed,
    EmitSubscriptionChanged,
    IssueRefund,
)

log = structlog.get_logger()

GATEWAY_CANCEL = "gateway.cancel"
REFUND_ISSUE = "refund.issue"


@dataclass(frozen=True, slots=True)
class EffectContext:
    """What the pure domain effects cannot know."""

    failed_payment: GatewayPayment | None = None
    next_retry_at: datetime | None = None
    refund_payment_id: UUID | None = None  # our payments.id of the period being refunded
    refund_key: str | None = None


async def run_effects(
    uow: UnitOfWork,
    effects: Sequence[Effect],
    before: SubscriptionRecord,
    after: SubscriptionRecord,
    account: BillingAccount,
    ctx: EffectContext,
    now: datetime,
) -> None:
    for effect in effects:
        match effect:
            case EmitSubscriptionChanged():
                new_plan = await uow.plans.by_id(after.state.plan_version_id)
                old_plan = await uow.plans.by_id(before.state.plan_version_id)
                assert new_plan is not None
                await uow.publisher.enqueue(
                    SubscriptionChangedEvent(
                        billing_account_id=account.id,
                        owner_type=account.owner_type,
                        owner_id=account.owner_id,
                        subscription_id=after.id,
                        plan_code=new_plan.plan.plan_code,
                        status=after.state.status,
                        previous_plan_code=old_plan.plan.plan_code if old_plan else None,
                        previous_status=before.state.status,
                        current_period_end=after.state.current_period_end,
                    )
                )
            case EmitPaymentFailed():
                failed = ctx.failed_payment
                plan = await uow.plans.by_id(after.state.plan_version_id)
                assert plan is not None
                price = (
                    plan.plan.price_month
                    if after.state.interval is Interval.MONTH
                    else plan.plan.price_year
                )
                await uow.publisher.enqueue(
                    PaymentFailedEvent(
                        billing_account_id=account.id,
                        subscription_id=after.id,
                        gateway_payment_id=failed.id if failed else "",
                        amount=failed.amount if failed else Paise(int(price or 0)),
                        reason="charge_failed",
                        next_retry_at=ctx.next_retry_at,
                    )
                )
            case CancelMandate():
                await uow.jobs.enqueue(
                    GATEWAY_CANCEL,
                    {
                        "gateway_subscription_id": effect.gateway_subscription_id,
                        "at_cycle_end": effect.at_cycle_end,
                    },
                    now,
                    f"{GATEWAY_CANCEL}:{effect.gateway_subscription_id}:{int(effect.at_cycle_end)}",
                )
            case IssueRefund():
                if ctx.refund_payment_id is None or ctx.refund_key is None:
                    log.warning("refund_skipped_no_payment", subscription_id=str(after.id))
                    continue
                await uow.jobs.enqueue(
                    REFUND_ISSUE,
                    {
                        "subscription_id": str(after.id),
                        "payment_id": str(ctx.refund_payment_id),
                        "amount": int(effect.amount),
                        "reason": effect.reason,
                        "refund_key": ctx.refund_key,
                        # the old period's interval decides revenue vs deferred revenue
                        "interval": before.state.interval.value,
                    },
                    now,
                    f"{REFUND_ISSUE}:{ctx.refund_key}",
                )
