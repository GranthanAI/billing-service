from datetime import timedelta
from typing import Any

import structlog

from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

KIND = "holds.sweep"
SCHEDULE = Recurring(KIND, every=timedelta(seconds=30))
BATCH = 500
log = structlog.get_logger()


@job_handler(KIND)
async def sweep_expired_holds(payload: dict[str, Any], ctx: JobContext) -> None:
    """Give back every message whose hold ran out (Decision 7): the pipeline failed, not the
    user. Usage that arrives later is counted and billed then, by commit.lua."""
    counters = ctx.counters
    if counters is None:
        raise RuntimeError("holds.sweep needs a Valkey-backed Counters in the job context")
    expired = 0
    while True:
        due = await counters.due_holds(int(ctx.clock.now().timestamp()), BATCH)
        if not due:
            break
        for user_id, hold_id in due:
            await counters.release(user_id, hold_id, "expire")
        expired += len(due)
    if expired:
        log.warning("holds_expired", count=expired)
