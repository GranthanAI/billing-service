"""The one place a subscription's status changes: domain transition + history + effects."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

import structlog

from billing.app.billing_emails import enqueue_email, iso
from billing.app.config import BillingContext
from billing.app.effects import EffectContext, run_effects
from billing.app.entitlement_events import announce_owner
from billing.app.ports import UnitOfWork
from billing.app.records import BillingAccount, SubscriptionHistoryEntry, SubscriptionRecord
from billing.app.revenue import release_deferred
from billing.domain.catalog import Interval, OwnerType
from billing.domain.subscription import (
    CancelledExternally,
    Effect,
    EmitPaymentFailed,
    GraceExpired,
    IllegalTransition,
    SubStatus,
    Trigger,
    UserCancelled,
    transition,
)

log = structlog.get_logger()

__all__ = ["IllegalTransition", "apply_transition"]


async def _transition_emails(
    uow: UnitOfWork,
    bctx: BillingContext,
    trigger: Trigger,
    effects: Sequence[Effect],
    before: SubscriptionRecord,
    after: SubscriptionRecord,
    account: BillingAccount,
    ctx: EffectContext | None,
    now: datetime,
) -> None:
    """Emails the transition calls for, queued in the transition's own transaction."""
    period = iso(after.state.current_period_end)
    entity = f"{after.id}:{period}"
    plan = await uow.plans.by_id(before.state.plan_version_id)
    plan_code = plan.plan.plan_code if plan else ""
    if any(isinstance(e, EmitPaymentFailed) for e in effects):
        price = (
            (
                plan.plan.price_month
                if after.state.interval is Interval.MONTH
                else plan.plan.price_year
            )
            if plan
            else 0
        )
        failed = ctx.failed_payment if ctx else None
        await enqueue_email(
            uow, bctx.config, "payment_failed", entity, account,
            {
                "subscription_id": str(after.id),
                "plan_code": plan_code,
                "amount_paise": int(failed.amount) if failed else int(price or 0),
                "grace_until": iso(after.state.grace_until),
                "next_retry_at": iso(ctx.next_retry_at) if ctx else None,
            },
            now,
        )  # fmt: skip
    if isinstance(trigger, GraceExpired):
        await enqueue_email(
            uow, bctx.config, "subscription_suspended", entity, account,
            {"subscription_id": str(after.id), "plan_code": plan_code}, now,
        )  # fmt: skip
    if isinstance(trigger, UserCancelled | CancelledExternally):
        await enqueue_email(
            uow, bctx.config, "subscription_cancelled", entity, account,
            {
                "plan_code": plan_code,
                "access_until": period,
                "by": "user" if isinstance(trigger, UserCancelled) else "payment_app",
            },
            now,
        )  # fmt: skip


async def apply_transition(
    uow: UnitOfWork,
    bctx: BillingContext,
    sub: SubscriptionRecord,
    trigger: Trigger,
    *,
    cause_ref: str | None,
    ctx: EffectContext | None = None,
    grace_days: int | None = None,
) -> SubscriptionRecord:
    """Raises IllegalTransition (before writing anything) when the trigger does not fit."""
    now = bctx.clock.now()
    result = transition(
        sub.state,
        trigger,
        now,
        grace_days=bctx.config.grace_days if grace_days is None else grace_days,
        grace_seconds=bctx.config.grace_period_seconds,
    )
    saved = await uow.subscriptions.save(replace(sub, state=result.state))
    name = type(trigger).__name__
    await uow.subscriptions.append_history(
        SubscriptionHistoryEntry(sub.id, sub.state.status, result.state.status, name, cause_ref)
    )
    account = await uow.accounts.get(sub.billing_account_id)
    assert account is not None
    await run_effects(uow, result.effects, sub, saved, account, ctx or EffectContext(), now)
    if (
        saved.state.status is SubStatus.ENDED
        and sub.state.status is not SubStatus.ENDED
        and sub.state.interval is Interval.YEAR
    ):  # nothing more is delivered for this payment: the rest of its deferred revenue is earned
        latest = await uow.payments.latest_for_subscription(sub.id)
        if latest is not None:
            await release_deferred(uow, latest, now)
        for seat_payment in await uow.payments.seat_payments(sub.id):
            await release_deferred(uow, seat_payment, now)
    await announce_owner(uow, account.owner_type, account.owner_id, "subscription", now)
    await _transition_emails(uow, bctx, trigger, result.effects, sub, saved, account, ctx, now)
    if account.owner_type is OwnerType.USER:
        owner_id = account.owner_id
        uow.after_commit(lambda: bctx.invalidate_entitlement(owner_id))
    elif bctx.invalidate_org is not None:
        invalidate_org = bctx.invalidate_org
        org_id = account.owner_id

        async def clear_members() -> None:
            await invalidate_org(org_id)

        uow.after_commit(clear_members)
    log.info(
        "subscription_transitioned",
        subscription_id=str(sub.id),
        trigger=name,
        from_status=sub.state.status.value,
        to_status=result.state.status.value,
    )
    return saved
