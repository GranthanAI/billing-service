"""The scheduled side of a subscription's life: grace expiry, period close, renewal reminders and
the poll that catches webhooks which never arrived. Every method is safe to run twice."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from billing.app.config import BillingContext
from billing.app.errors import GatewayRejected, GatewayUnavailable
from billing.app.events import RenewalUpcomingEvent
from billing.app.ports import GatewaySubscription
from billing.app.records import SubscriptionRecord
from billing.app.transitions import IllegalTransition, apply_transition
from billing.app.webhooks import PROCESSED, EventApplier, GatewayEvent
from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.subscription import GraceExpired, PeriodEnded, Trigger

log = structlog.get_logger()

BATCH = 100
POLL_AFTER = timedelta(hours=2)
POLL_BATCH = 200
POLL_CALLS_PER_SECOND = 5.0
REMIND_FROM, REMIND_TO = timedelta(hours=24), timedelta(hours=48)
REMINDER_CONSUMER = "renewal-remind"
STATUS_EVENTS = {
    "pending": "subscription.pending",
    "paused": "subscription.paused",
    "halted": "subscription.halted",
    "cancelled": "subscription.cancelled",
}


class RateLimiter:
    """At most `per_second` calls a second: each call waits for its slot."""

    def __init__(
        self,
        per_second: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gap = 1.0 / per_second
        self._sleep = sleep
        self._monotonic = monotonic
        self._next = 0.0

    async def wait(self, calls: int = 1) -> None:
        now = self._monotonic()
        start = max(now, self._next)
        self._next = start + calls * self._gap
        if start > now:
            await self._sleep(start - now)


class Lifecycle:
    def __init__(
        self,
        bctx: BillingContext,
        applier: EventApplier,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._b = bctx
        self._applier = applier
        self._limiter = limiter or RateLimiter(POLL_CALLS_PER_SECOND)

    # --- grace.expire and period.close ------------------------------------------------------

    async def _transition_due(
        self, due: Callable[[], Awaitable[list[SubscriptionRecord]]], trigger: Trigger, cause: str
    ) -> int:
        done = 0
        while True:
            batch = await due()
            moved = 0
            for rec in batch:
                async with self._b.uow_factory() as uow:
                    locked = await uow.subscriptions.lock(rec.id)
                    if locked is None:
                        continue
                    try:  # the guard rechecks the time against the locked row
                        await apply_transition(uow, self._b, locked, trigger, cause_ref=cause)
                        moved += 1
                    except IllegalTransition:
                        continue  # it recovered, or somebody else got there first
            done += moved
            if moved == 0 or len(batch) < BATCH:
                return done

    async def expire_grace(self) -> int:
        """`past_due` past `grace_until` becomes `suspended`."""

        async def due() -> list[SubscriptionRecord]:
            async with self._b.uow_factory() as uow:
                return list(await uow.subscriptions.list_grace_due(self._b.clock.now(), BATCH))

        return await self._transition_due(due, GraceExpired(), "grace.expire")

    async def close_periods(self) -> int:
        """`cancelling` past its period end becomes `ended`."""

        async def due() -> list[SubscriptionRecord]:
            async with self._b.uow_factory() as uow:
                return list(await uow.subscriptions.list_cancelling_due(self._b.clock.now(), BATCH))

        return await self._transition_due(due, PeriodEnded(), "period.close")

    # --- renewal.remind ---------------------------------------------------------------------

    async def remind_renewals(self) -> int:
        """One `subscription.renewal_upcoming` per subscription per period, for renewals 24 to 48
        hours away. The claim and the outbox write share a transaction, so a rerun adds nothing."""
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            due = list(
                await uow.subscriptions.list_renewing(now + REMIND_FROM, now + REMIND_TO, 1000)
            )
        sent = 0
        for rec in due:
            period_end = rec.state.current_period_end
            assert period_end is not None
            async with self._b.uow_factory() as uow:
                if not await uow.processed.claim(
                    REMINDER_CONSUMER, f"{rec.id}:{period_end:%Y%m%dT%H%M}"
                ):
                    continue
                account = await uow.accounts.get(rec.billing_account_id)
                # the plan that will be charged: a scheduled downgrade has already taken over
                plan = await uow.plans.by_id(
                    rec.state.pending_plan_version_id or rec.state.plan_version_id
                )
                if account is None or plan is None:
                    continue
                interval = rec.state.pending_interval or rec.state.interval
                price = (
                    plan.plan.price_month if interval is Interval.MONTH else plan.plan.price_year
                )
                await uow.publisher.enqueue(
                    RenewalUpcomingEvent(
                        billing_account_id=account.id,
                        owner_id=account.owner_id,
                        plan_code=plan.plan.plan_code,
                        amount=Paise(int(price or 0)),
                        charge_at=period_end,
                    )
                )
                sent += 1
        return sent

    # --- subscription.poll ------------------------------------------------------------------

    async def poll(self) -> int:
        """Ask Razorpay about live subscriptions whose period ended over 2 hours ago with no
        charge recorded, and apply what it shows (the only backstop for lost webhooks)."""
        cutoff = self._b.clock.now() - POLL_AFTER
        async with self._b.uow_factory() as uow:
            stale = list(await uow.subscriptions.list_stale_live(cutoff, POLL_BATCH))
        applied = 0
        for rec in stale:
            try:
                applied += await self._poll_one(rec)
            except (GatewayRejected, GatewayUnavailable) as exc:
                log.warning("poll_gateway_error", subscription_id=str(rec.id), error=str(exc))
            except Exception:
                log.exception("poll_failed", subscription_id=str(rec.id))
        return applied

    async def _poll_one(self, rec: SubscriptionRecord) -> int:
        mandates = [rec.state.gateway_subscription_id, rec.state.next_gateway_subscription_id]
        applied = 0
        for gid in (m for m in mandates if m):
            await self._limiter.wait()
            fresh = await self._b.gateway.fetch_subscription(gid)
            if await self._missed_charge(rec, gid, fresh):
                applied += 1
            if (kind := STATUS_EVENTS.get(fresh.status)) is not None:
                event = GatewayEvent(f"poll:{gid}:{fresh.status}", kind, fresh)
                if await self._applier.apply_gateway_event(event) == PROCESSED:
                    applied += 1
        return applied

    async def _missed_charge(
        self, rec: SubscriptionRecord, gid: str, fresh: GatewaySubscription
    ) -> bool:
        if fresh.paid_count == 0:
            return False
        is_next = gid == rec.state.next_gateway_subscription_id
        ours = rec.state.current_period_end
        if not is_next and (fresh.current_end is None or (ours and fresh.current_end <= ours)):
            return False  # Razorpay has not moved past the period we already hold
        await self._limiter.wait()
        payments = await self._b.gateway.list_subscription_payments(gid)
        await self._limiter.wait(len(payments))  # one more call per payment it just fetched
        if not payments:
            return False
        newest = payments[0]
        async with self._b.uow_factory() as uow:
            if await uow.payments.by_gateway_id(newest.id) is not None:
                return False
        event = GatewayEvent(f"poll:{newest.id}", "subscription.charged", fresh, None, newest)
        return await self._applier.apply_gateway_event(event) == PROCESSED
