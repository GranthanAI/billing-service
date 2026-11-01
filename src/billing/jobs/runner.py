"""Durable job runner: claims jobs from the jobs table and runs registered handlers.

Handlers must be idempotent: a job can run twice after a crash (the reaper requeues it).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from billing.adapters.db.uow import SqlUnitOfWork
from billing.app.ports import Clock, Counters, IdGenerator, PlanGateway
from billing.app.records import Job, JobStatus
from billing.observability import JOBS_DEAD

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class JobContext:
    uow_factory: Callable[[], SqlUnitOfWork]
    clock: Clock
    ids: IdGenerator
    gateway: PlanGateway | None = None
    counters: Counters | None = None


Handler = Callable[[dict[str, Any], JobContext], Awaitable[None]]
REGISTRY: dict[str, Handler] = {}


def job_handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        REGISTRY[kind] = fn
        return fn

    return register


class JobRunner:
    def __init__(
        self,
        ctx: JobContext,
        *,
        concurrency: int = 4,
        poll_interval: float = 1.0,
        lock_seconds: int = 300,
        handler_timeout: float = 270.0,
        reap_every: float = 30.0,
        registry: dict[str, Handler] | None = None,
    ) -> None:
        self._ctx = ctx
        self._concurrency = concurrency
        self._poll = poll_interval
        self._lock_seconds = lock_seconds
        self._timeout = handler_timeout
        self._reap_every = reap_every
        self._registry = REGISTRY if registry is None else registry
        self._running: set[asyncio.Task[None]] = set()

    async def _execute(self, job: Job) -> None:
        handler = self._registry.get(job.kind)
        try:
            if handler is None:
                raise LookupError(f"no handler for job kind {job.kind!r}")
            await asyncio.wait_for(handler(job.payload, self._ctx), self._timeout)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            async with self._ctx.uow_factory() as uow:
                status = await uow.jobs.fail(job.id, message, self._ctx.clock.now())
            if status is JobStatus.DEAD:
                JOBS_DEAD.labels(kind=job.kind).inc()
                log.error("job_dead", kind=job.kind, id=job.id, error=message)
            else:
                log.warning("job_failed", kind=job.kind, id=job.id, error=message)
        else:
            async with self._ctx.uow_factory() as uow:
                await uow.jobs.complete(job.id)

    async def claim_and_start(self) -> int:
        free = self._concurrency - len(self._running)
        if free <= 0:
            return 0
        async with self._ctx.uow_factory() as uow:
            jobs = await uow.jobs.claim(free, self._lock_seconds, self._ctx.clock.now())
        for job in jobs:
            task = asyncio.create_task(self._execute(job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return len(jobs)

    async def reap(self) -> int:
        async with self._ctx.uow_factory() as uow:
            return await uow.jobs.reap(self._ctx.clock.now())

    async def _sleep(self, stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def _reaper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if n := await self.reap():
                    log.warning("jobs_reaped", count=n)
            except Exception:
                log.exception("reap_failed")
            await self._sleep(stop, self._reap_every)

    async def run(self, stop: asyncio.Event, drain_timeout: float = 30.0) -> None:
        """Claim until `stop` is set, then wait for running jobs to finish."""
        reaper = asyncio.create_task(self._reaper(stop))
        try:
            while not stop.is_set():
                try:
                    started = await self.claim_and_start()
                except Exception:
                    log.exception("claim_failed")
                    started = 0
                if started == 0 or len(self._running) >= self._concurrency:
                    await self._sleep(stop, self._poll)
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            if self._running:
                _, pending = await asyncio.wait(self._running, timeout=drain_timeout)
                for task in pending:  # still running after the drain window: give up on them;
                    task.cancel()  # the reaper requeues the job once its lock expires
