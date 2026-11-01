from typing import Any

import structlog

from billing.adapters.db import partitions
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

KIND = "partitions.ensure"
SCHEDULE = Recurring(KIND, daily_at="04:00 IST")
log = structlog.get_logger()


@job_handler(KIND)
async def ensure_usage_partitions(payload: dict[str, Any], ctx: JobContext) -> None:
    async with ctx.uow_factory() as uow:
        created = await partitions.ensure(uow.connection, months_ahead=2)
    log.info("usage_partitions_ensured", created=created)
