"""Scheduled jobs for the subscription lifecycle (all times IST) and housekeeping."""

from datetime import timedelta
from typing import Any

import structlog

from billing.app.lifecycle import Lifecycle
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

GRACE_EXPIRE = "grace.expire"
PERIOD_CLOSE = "period.close"
SUBSCRIPTION_POLL = "subscription.poll"
RENEWAL_REMIND = "renewal.remind"
HOUSEKEEPING = "housekeeping"

SCHEDULES = (
    Recurring(GRACE_EXPIRE, every=timedelta(minutes=15)),
    Recurring(PERIOD_CLOSE, every=timedelta(minutes=15)),
    Recurring(SUBSCRIPTION_POLL, every=timedelta(hours=1)),
    Recurring(RENEWAL_REMIND, daily_at="10:00 IST"),
    Recurring(HOUSEKEEPING, daily_at="04:30 IST"),
)
log = structlog.get_logger()


def _lifecycle(ctx: JobContext) -> Lifecycle:
    if ctx.billing is None or ctx.applier is None:
        raise RuntimeError("lifecycle jobs need the billing context in the job context")
    return Lifecycle(ctx.billing, ctx.applier)


@job_handler(GRACE_EXPIRE)
async def grace_expire(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _lifecycle(ctx).expire_grace():
        log.info("grace_expired", count=n)


@job_handler(PERIOD_CLOSE)
async def period_close(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _lifecycle(ctx).close_periods():
        log.info("periods_closed", count=n)


@job_handler(SUBSCRIPTION_POLL)
async def subscription_poll(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _lifecycle(ctx).poll():
        log.warning("poll_recovered_events", count=n)  # each one is a webhook we never got


@job_handler(RENEWAL_REMIND)
async def renewal_remind(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _lifecycle(ctx).remind_renewals():
        log.info("renewal_reminders", count=n)


@job_handler(HOUSEKEEPING)
async def housekeeping(payload: dict[str, Any], ctx: JobContext) -> None:
    async with ctx.uow_factory() as uow:
        removed = await uow.housekeeping.prune(ctx.clock.now())
    log.info("housekeeping_done", **removed)
