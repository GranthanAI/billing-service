"""checkout.reconcile: catch checkouts whose webhooks never arrived, and end abandoned ones."""

from datetime import timedelta

import structlog

from billing.app.config import BillingContext
from billing.app.effects import GATEWAY_CANCEL
from billing.app.records import ChangeRecord, ChangeStatus, SubscriptionRecord
from billing.app.transitions import IllegalTransition, apply_transition
from billing.app.webhooks import EventApplier, GatewayEvent
from billing.domain.subscription import AuthorisationTimedOut

log = structlog.get_logger()

MIN_AGE = timedelta(minutes=15)
BATCH = 100
AUTHORISED = ("authenticated", "active")


class CheckoutReconciler:
    def __init__(self, bctx: BillingContext, applier: EventApplier) -> None:
        self._b = bctx
        self._applier = applier

    async def run(self) -> int:
        """Returns how many checkouts or changes it acted on."""
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            subs = await uow.subscriptions.list_incomplete_before(now - MIN_AGE, BATCH)
            changes = await uow.changes.list_pending_before(now - MIN_AGE, BATCH)
        acted = 0
        for sub in subs:
            try:
                acted += await self._subscription(sub)
            except Exception:
                log.exception("reconcile_subscription_failed", subscription_id=str(sub.id))
        for change in changes:
            try:
                acted += await self._change(change)
            except Exception:
                log.exception("reconcile_change_failed", change_id=str(change.id))
        return acted

    def _abandoned(self, created_at: object) -> bool:
        cutoff = self._b.clock.now() - timedelta(hours=self._b.config.abandon_hours)
        return created_at is not None and created_at < cutoff  # type: ignore[operator]

    async def _subscription(self, sub: SubscriptionRecord) -> int:
        gid = sub.state.gateway_subscription_id
        old = self._abandoned(sub.created_at)
        if gid is None:  # we crashed before storing the mandate; nothing exists to cancel
            return await self._time_out(sub) if old else 0
        fresh = await self._b.gateway.fetch_subscription(gid)
        if fresh.paid_count > 0:
            payments = await self._b.gateway.list_subscription_payments(gid)
            if not payments:
                return 0
            first = payments[-1]
            await self._applier.apply_gateway_event(
                GatewayEvent(f"reconcile:{first.id}", "subscription.charged", fresh, None, first)
            )
            return 1
        if old and fresh.status not in AUTHORISED:
            return await self._time_out(sub)
        return 0

    async def _time_out(self, sub: SubscriptionRecord) -> int:
        async with self._b.uow_factory() as uow:
            locked = await uow.subscriptions.lock(sub.id)
            if locked is None:
                return 0
            try:
                await apply_transition(
                    uow, self._b, locked, AuthorisationTimedOut(), cause_ref="checkout.reconcile"
                )
            except IllegalTransition:
                return 0
        return 1

    async def _change(self, change: ChangeRecord) -> int:
        fresh = await self._b.gateway.fetch_subscription(change.gateway_subscription_id)
        if fresh.paid_count > 0:
            payments = await self._b.gateway.list_subscription_payments(fresh.id)
            if not payments:
                return 0
            first = payments[-1]
            await self._applier.apply_gateway_event(
                GatewayEvent(f"reconcile:{first.id}", "subscription.charged", fresh, None, first)
            )
            return 1
        if fresh.status in AUTHORISED:
            await self._applier.apply_gateway_event(
                GatewayEvent(f"reconcile:{fresh.id}", "subscription.authenticated", fresh)
            )
            return 1
        if self._abandoned(change.created_at):
            async with self._b.uow_factory() as uow:
                await uow.changes.set_status(change.id, ChangeStatus.ABANDONED)
                await uow.jobs.enqueue(
                    GATEWAY_CANCEL,
                    {
                        "gateway_subscription_id": change.gateway_subscription_id,
                        "at_cycle_end": False,
                    },
                    self._b.clock.now(),
                    f"{GATEWAY_CANCEL}:{change.gateway_subscription_id}:0",
                )
            return 1
        return 0
