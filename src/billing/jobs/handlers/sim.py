"""Payment-simulator jobs. Registered ONLY when PAYMENT_SIMULATION=true (see `register`).

They drive the simulated provider the way Razorpay's own systems would: renew subscriptions, send
the queued webhooks, process refunds and settle payments. Nothing here runs in production, because
the worker never calls `register` unless the simulator is the gateway.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import structlog

from billing.adapters.gateway_sim import SimulatorGateway
from billing.jobs.handlers import billing as billing_jobs
from billing.jobs.handlers import lifecycle as lifecycle_jobs
from billing.jobs.runner import REGISTRY, Handler, JobContext
from billing.jobs.scheduler import Recurring

RENEW = "sim.renew"
DELIVER = "sim.deliver_webhooks"
REFUNDS = "sim.refunds"
SETTLE = "sim.settle"
SCHEDULES = (
    Recurring(RENEW, every=timedelta(seconds=30)),
    Recurring(DELIVER, every=timedelta(seconds=2)),
    Recurring(REFUNDS, every=timedelta(seconds=5)),
    Recurring(SETTLE, every=timedelta(seconds=60)),
)
# SIM_FAST_JOBS: the lifecycle, grace, poll and reconcile jobs run this often
FAST_KINDS = frozenset(
    {
        lifecycle_jobs.GRACE_EXPIRE,
        lifecycle_jobs.PERIOD_CLOSE,
        lifecycle_jobs.SUBSCRIPTION_POLL,
        billing_jobs.RECONCILE,
    }
)
FAST_EVERY = timedelta(seconds=30)
log = structlog.get_logger()


def _sim(ctx: JobContext) -> SimulatorGateway:
    if not isinstance(ctx.gateway, SimulatorGateway):
        raise RuntimeError("sim jobs run only with PAYMENT_SIMULATION=true")
    return ctx.gateway


async def renew(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _sim(ctx).renew_due():
        log.info("sim_renewed", count=n)


async def deliver_webhooks(payload: dict[str, Any], ctx: JobContext) -> None:
    await _sim(ctx).deliver_due()


async def refunds(payload: dict[str, Any], ctx: JobContext) -> None:
    if n := await _sim(ctx).process_refunds():
        log.info("sim_refunds_processed", count=n)


async def settle(payload: dict[str, Any], ctx: JobContext) -> None:
    if setl := await _sim(ctx).settle(everything=bool(payload.get("everything"))):
        log.info("sim_settled", settlement_id=setl)


HANDLERS: dict[str, Handler] = {
    RENEW: renew,
    DELIVER: deliver_webhooks,
    REFUNDS: refunds,
    SETTLE: settle,
}


def register() -> tuple[Recurring, ...]:
    """Add the sim handlers to the job registry and return their schedules."""
    REGISTRY.update(HANDLERS)
    return SCHEDULES


def fast_schedule(schedule: Sequence[Recurring]) -> list[Recurring]:
    """`schedule` with the lifecycle, grace, poll and reconcile jobs every 30 seconds."""
    return [Recurring(j.kind, every=FAST_EVERY) if j.kind in FAST_KINDS else j for j in schedule]
