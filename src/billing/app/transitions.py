"""The one place a subscription's status changes: domain transition + history + effects."""

from dataclasses import replace

import structlog

from billing.app.config import BillingContext
from billing.app.effects import EffectContext, run_effects
from billing.app.ports import UnitOfWork
from billing.app.records import SubscriptionHistoryEntry, SubscriptionRecord
from billing.app.revenue import release_deferred
from billing.domain.catalog import Interval, OwnerType
from billing.domain.subscription import IllegalTransition, SubStatus, Trigger, transition

log = structlog.get_logger()

__all__ = ["IllegalTransition", "apply_transition"]


async def apply_transition(
    uow: UnitOfWork,
    bctx: BillingContext,
    sub: SubscriptionRecord,
    trigger: Trigger,
    *,
    cause_ref: str | None,
    ctx: EffectContext | None = None,
) -> SubscriptionRecord:
    """Raises IllegalTransition (before writing anything) when the trigger does not fit."""
    now = bctx.clock.now()
    result = transition(sub.state, trigger, now, grace_days=bctx.config.grace_days)
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
    if account.owner_type is OwnerType.USER:
        owner_id = account.owner_id
        uow.after_commit(lambda: bctx.invalidate_entitlement(owner_id))
    log.info(
        "subscription_transitioned",
        subscription_id=str(sub.id),
        trigger=name,
        from_status=sub.state.status.value,
        to_status=result.state.status.value,
    )
    return saved
