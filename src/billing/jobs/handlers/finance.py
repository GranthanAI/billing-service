"""settlements.sync (daily 06:00 IST) and revenue.recognise (1st of the month, 02:00 IST)."""

from datetime import date, timedelta
from typing import Any

import structlog

from billing.app.settlements import RevenueService, SettlementService
from billing.domain.calendar import ist_day
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

SETTLEMENTS_SYNC = "settlements.sync"
REVENUE_RECOGNISE = "revenue.recognise"
SCHEDULES = (
    Recurring(SETTLEMENTS_SYNC, daily_at="06:00 IST"),
    Recurring(REVENUE_RECOGNISE, monthly_on=1, at="02:00 IST"),
)
log = structlog.get_logger()


@job_handler(SETTLEMENTS_SYNC)
async def settlements_sync(payload: dict[str, Any], ctx: JobContext) -> None:
    """The previous IST day (or `payload.day`). Idempotent: ledger keys are per payment."""
    if ctx.billing is None:
        raise RuntimeError("settlements.sync needs the billing context")
    day = (
        date.fromisoformat(payload["day"])
        if payload.get("day")
        else ist_day(ctx.clock.now()) - timedelta(days=1)
    )
    await SettlementService(ctx.uow_factory, ctx.billing.gateway, ctx.clock).sync(day)


@job_handler(REVENUE_RECOGNISE)
async def revenue_recognise(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await RevenueService(ctx.uow_factory, ctx.clock).recognise():
        log.info("revenue_recognised", subscriptions=n)
