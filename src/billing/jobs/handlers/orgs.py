"""org.renewal (daily 07:00 IST): invoice organisations for next year and chase overdue ones."""

from typing import Any

import structlog

from billing.app.org_billing import OrgBilling
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

ORG_RENEWAL = "org.renewal"
SCHEDULE = Recurring(ORG_RENEWAL, daily_at="07:00 IST")
log = structlog.get_logger()


@job_handler(ORG_RENEWAL)
async def org_renewal(payload: dict[str, Any], ctx: JobContext) -> None:
    if ctx.billing is None:
        raise RuntimeError("org.renewal needs the billing context")
    issued, overdue = await OrgBilling(ctx.billing).renew()
    if issued or overdue:
        log.info("org_renewal", invoices_issued=issued, marked_past_due=overdue)
