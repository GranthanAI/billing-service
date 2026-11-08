"""entitlements.fanout: one `entitlements.changed` per member of an organisation."""

from typing import Any
from uuid import UUID

import structlog

from billing.app.entitlement_events import FANOUT_JOB, announce
from billing.jobs.runner import JobContext, job_handler

log = structlog.get_logger()


@job_handler(FANOUT_JOB)
async def fanout(payload: dict[str, Any], ctx: JobContext) -> None:
    """Needs org-service for the member list; without it nobody is a member of anything."""
    if ctx.orgs is None:
        return
    members = await ctx.orgs.members(UUID(payload["org_id"]))  # outside the transaction
    async with ctx.uow_factory() as uow:
        for member in members:
            await announce(uow, member, str(payload["reason"]))
    log.info("entitlements_fanout", org_id=payload["org_id"], members=len(members))
