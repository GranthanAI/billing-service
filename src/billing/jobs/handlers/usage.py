"""usage.rollup: recompute yesterday's usage_daily in full, every day at 00:30 IST."""

from datetime import date, timedelta
from typing import Any

import structlog

from billing.domain.calendar import ist_day
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

KIND = "usage.rollup"
SCHEDULE = Recurring(KIND, daily_at="00:30 IST")
log = structlog.get_logger()


@job_handler(KIND)
async def usage_rollup(payload: dict[str, Any], ctx: JobContext) -> None:
    """Idempotent: the rows are recomputed from usage_events (append-only), so running it twice
    leaves the same rows. `payload["day"]` (YYYY-MM-DD) rolls up a specific IST day instead."""
    day = (
        date.fromisoformat(payload["day"])
        if payload.get("day")
        else ist_day(ctx.clock.now()) - timedelta(days=1)
    )
    async with ctx.uow_factory() as uow:
        rows = await uow.usage.rollup_day(day)
    log.info("usage_rolled_up", day=day.isoformat(), rows=rows)
