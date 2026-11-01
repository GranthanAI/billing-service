"""Outbox relay: publishes committed outbox rows to Kafka, in id order.

One replica relays at a time: it holds a session-level advisory lock on a dedicated connection
for as long as it runs; the others retry the lock every few seconds.

Known gap (no fix needed): ids are allocated at insert but transactions commit in any order, so
a lower id can become visible after a higher one was already published. Rows are never lost,
only reordered. Per-key order still holds because writers of the same subscription lock the
same subscription row, so their outbox rows commit in the order they were inserted.

Delivery is at-least-once: a crash after flush and before `published_at` is set republishes the
batch, and consumers drop the duplicates through processed_events.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.kafka.producer import KafkaProducer
from billing.app.ports import Clock, IdGenerator
from billing.observability import OUTBOX_OLDEST_UNPUBLISHED

log = structlog.get_logger()
LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtext('outbox-relay'))")


class OutboxRelay:
    def __init__(
        self,
        engine: AsyncEngine,
        ids: IdGenerator,
        clock: Clock,
        producer: KafkaProducer,
        *,
        batch_size: int = 500,
        idle_sleep: float = 0.2,
        lock_retry: float = 5.0,
        after_flush: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._engine = engine
        self._ids = ids
        self._clock = clock
        self._producer = producer
        self._batch = batch_size
        self._idle = idle_sleep
        self._lock_retry = lock_retry
        self._after_flush = after_flush  # test hook: raise here to simulate a crash
        self.holds_lock = False

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def _acquire(self, stop: asyncio.Event) -> AsyncConnection | None:
        while not stop.is_set():
            conn = await self._engine.connect()
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            if (await conn.execute(LOCK_SQL)).scalar_one():
                return conn
            await conn.close()
            await self._wait(stop, self._lock_retry)
        return None

    async def relay_once(self) -> int:
        """Publish one batch. Returns how many rows were sent."""
        async with SqlUnitOfWork(self._engine, self._ids, self._clock) as uow:
            outbox = uow.publisher
            rows = await outbox.fetch_unpublished(self._batch)
            now = self._clock.now()
            OUTBOX_OLDEST_UNPUBLISHED.set(await outbox.oldest_unpublished_age(now))
            if not rows:
                return 0
            futures = [
                await self._producer.send(r.topic, r.key.encode(), r.payload, r.headers)
                for r in rows
            ]
            await self._producer.flush()
            await asyncio.gather(*futures)
            if self._after_flush is not None:
                await self._after_flush()
            await outbox.mark_published([r.id for r in rows], self._clock.now())
            return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            lock_conn = await self._acquire(stop)
            if lock_conn is None:
                return
            self.holds_lock = True
            log.info("outbox_relay_started")
            try:
                while not stop.is_set():
                    sent = await self.relay_once()
                    if sent == 0:
                        await self._wait(stop, self._idle)
            except Exception:
                log.exception("outbox_relay_failed")
                await self._wait(stop, self._lock_retry)
            finally:
                self.holds_lock = False
                with contextlib.suppress(Exception):
                    await lock_conn.close()  # closing the session releases the advisory lock
