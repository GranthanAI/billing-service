"""Recurring jobs. Every replica ticks; a dedupe key per slot makes each slot enqueue once."""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import structlog

from billing.adapters.db.uow import SqlUnitOfWork
from billing.app.ports import Clock
from billing.domain.calendar import IST, require_aware

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Recurring:
    kind: str
    every: timedelta | None = None
    daily_at: str | None = None  # "04:00 IST"
    monthly_on: int | None = None  # day of month, IST
    at: str | None = None  # "02:00 IST", with monthly_on

    def __post_init__(self) -> None:
        chosen = [self.every is not None, self.daily_at is not None, self.monthly_on is not None]
        if sum(chosen) != 1:
            raise ValueError("give exactly one of every, daily_at or monthly_on")
        if self.monthly_on is not None and not 1 <= self.monthly_on <= 28:
            raise ValueError("monthly_on must be 1..28 so every month has the day")


def _clock_time(spec: str) -> time:
    hhmm, _, zone = spec.partition(" ")
    if zone != "IST":
        raise ValueError(f"only IST times are supported, got {spec!r}")
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def slot_start(job: Recurring, now: datetime) -> datetime:
    """Start of the slot `now` falls in, in UTC."""
    require_aware(now)
    if job.every is not None:
        step = int(job.every.total_seconds())
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(epoch - epoch % step, UTC)
    local = now.astimezone(IST)
    if job.daily_at is not None:
        start = datetime.combine(local.date(), _clock_time(job.daily_at), tzinfo=IST)
        if start > local:
            start -= timedelta(days=1)
        return start.astimezone(UTC)
    assert job.monthly_on is not None and job.at is not None
    at = _clock_time(job.at)
    start = datetime.combine(date(local.year, local.month, job.monthly_on), at, tzinfo=IST)
    if start > local:
        year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
        start = datetime.combine(date(year, month, job.monthly_on), at, tzinfo=IST)
    return start.astimezone(UTC)


def dedupe_key(job: Recurring, now: datetime) -> str:
    return f"{job.kind}:{slot_start(job, now).isoformat()}"


class Scheduler:
    def __init__(
        self,
        uow_factory: Callable[[], SqlUnitOfWork],
        clock: Clock,
        schedule: Sequence[Recurring],
        *,
        tick_seconds: float = 5.0,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock
        self._schedule = list(schedule)
        self._tick = tick_seconds

    async def tick(self) -> int:
        """Enqueue the current slot of every recurring job; returns how many were new.

        A freshly started deployment enqueues the slot it is in even if the slot began hours
        ago, so recurring handlers must be idempotent (they are, by rule).
        """
        now = self._clock.now()
        created = 0
        async with self._uow() as uow:
            for job in self._schedule:
                if await uow.jobs.enqueue(job.kind, {}, now, dedupe_key(job, now)):
                    created += 1
        return created

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._tick)
