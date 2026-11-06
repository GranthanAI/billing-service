"""Gauges that need a look at the database: inbox backlog and subscriptions by plan and status."""

import asyncio
import contextlib
from collections.abc import Callable

import structlog

from billing.app.ports import UnitOfWork
from billing.observability import INBOX_BACKLOG, SUBSCRIPTIONS

log = structlog.get_logger()
INBOX_STATUSES = ("received", "processed", "ignored", "failed")


async def refresh_gauges(uow_factory: Callable[[], UnitOfWork]) -> None:
    async with uow_factory() as uow:
        inbox, subs = await uow.admin.gauge_counts()
    for status in INBOX_STATUSES:
        INBOX_BACKLOG.labels(status=status).set(inbox.get(status, 0))
    SUBSCRIPTIONS.clear()  # a (plan, status) pair that dropped to zero must read 0, not stale
    for (plan, status), n in subs.items():
        SUBSCRIPTIONS.labels(plan=plan, status=status).set(n)


async def sample_gauges(
    uow_factory: Callable[[], UnitOfWork], stop: asyncio.Event, every: float = 30.0
) -> None:
    while not stop.is_set():
        try:
            await refresh_gauges(uow_factory)
        except Exception:
            log.exception("gauge_refresh_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)
