"""Job handlers: checkout.reconcile, gateway.cancel and refund.issue."""

from datetime import timedelta
from typing import Any

import structlog

from billing.app.charges import issue_refund
from billing.app.errors import GatewayRejected
from billing.app.reconcile import CheckoutReconciler
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

RECONCILE = "checkout.reconcile"
SCHEDULE = Recurring(RECONCILE, every=timedelta(minutes=5))
GONE = frozenset({"cancelled", "completed", "expired"})
log = structlog.get_logger()


def _need(ctx: JobContext) -> Any:
    if ctx.billing is None or ctx.applier is None:
        raise RuntimeError("this job needs the billing context in the job context")
    return ctx.billing


@job_handler(RECONCILE)
async def checkout_reconcile(payload: dict[str, Any], ctx: JobContext) -> None:
    bctx = _need(ctx)
    assert ctx.applier is not None
    acted = await CheckoutReconciler(bctx, ctx.applier).run()
    if acted:
        log.info("checkout_reconciled", count=acted)


@job_handler("gateway.cancel")
async def gateway_cancel(payload: dict[str, Any], ctx: JobContext) -> None:
    bctx = _need(ctx)
    gid = str(payload["gateway_subscription_id"])
    try:
        await bctx.gateway.cancel_subscription(gid, at_cycle_end=bool(payload["at_cycle_end"]))
    except GatewayRejected:
        # "already cancelled" is a 4xx too; it counts as done only if Razorpay says it is gone
        if (await bctx.gateway.fetch_subscription(gid)).status not in GONE:
            raise


@job_handler("refund.issue")
async def refund_issue(payload: dict[str, Any], ctx: JobContext) -> None:
    await issue_refund(_need(ctx), payload)
