"""reconcile.daily: 03:00 IST, for the previous IST day. Report only."""

from datetime import date, timedelta
from typing import Any

from billing.app.reconciliation import ReconciliationService
from billing.domain.calendar import ist_day
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

KIND = "reconcile.daily"
SCHEDULE = Recurring(KIND, daily_at="03:00 IST")


@job_handler(KIND)
async def reconcile_daily(payload: dict[str, Any], ctx: JobContext) -> None:
    """Idempotent: a rerun adds a new set of result rows (the latest one is what the admin API
    shows) and changes nothing else. `payload.day` (YYYY-MM-DD) reconciles another day."""
    if ctx.billing is None or ctx.counters is None:
        raise RuntimeError("reconcile.daily needs the billing context and the counters")
    day = (
        date.fromisoformat(payload["day"])
        if payload.get("day")
        else ist_day(ctx.clock.now()) - timedelta(days=1)
    )
    await ReconciliationService(ctx.uow_factory, ctx.billing.gateway, ctx.counters, ctx.clock).run(
        day
    )
