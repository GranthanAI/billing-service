"""Outbox, consumer inbox, idempotency keys, webhook inbox, jobs and admin audit."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import (
    admin_audit,
    idempotency_keys,
    jobs,
    outbox,
    processed_events,
    webhook_inbox,
)
from billing.adapters.kafka.codec import EventCodec
from billing.app.events import OutboxEvent
from billing.app.ports import Clock
from billing.app.records import (
    Begin,
    InboxRow,
    InProgress,
    Job,
    JobStatus,
    Mismatch,
    New,
    Replay,
)


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    topic: str
    key: str
    payload: bytes
    headers: dict[str, str]


class SqlOutbox:
    """The Publisher port: encodes and writes in the caller's transaction."""

    def __init__(self, conn: AsyncConnection, codec: EventCodec) -> None:
        self._c = conn
        self._codec = codec

    async def enqueue(self, event: OutboxEvent) -> None:
        e = self._codec.encode(event)
        await self._c.execute(
            sa.insert(outbox).values(
                topic=e.topic,
                key=e.key,
                event_type=e.event_type,
                payload=e.payload,
                headers=e.headers,
            )
        )

    # --- used by the relay (not part of the Publisher port) ---------------------------------

    async def fetch_unpublished(self, limit: int) -> list[OutboxRow]:
        q = (
            sa.select(outbox.c.id, outbox.c.topic, outbox.c.key, outbox.c.payload, outbox.c.headers)
            .where(outbox.c.published_at.is_(None))
            .order_by(outbox.c.id)
            .limit(limit)
        )
        return [
            OutboxRow(r.id, r.topic, r.key, bytes(r.payload), dict(r.headers))
            for r in (await self._c.execute(q)).all()
        ]

    async def mark_published(self, ids: list[int], now: datetime) -> None:
        await self._c.execute(
            sa.update(outbox).where(outbox.c.id.in_(ids)).values(published_at=now)
        )

    async def oldest_unpublished_age(self, now: datetime) -> float:
        oldest = (
            await self._c.execute(
                sa.select(sa.func.min(outbox.c.created_at)).where(outbox.c.published_at.is_(None))
            )
        ).scalar_one()
        return 0.0 if oldest is None else max(0.0, (now - oldest).total_seconds())


class SqlProcessedEvents:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def claim(self, consumer: str, event_id: str) -> bool:
        result = await self._c.execute(
            pg_insert(processed_events)
            .values(consumer=consumer, event_id=event_id)
            .on_conflict_do_nothing()
        )
        return result.rowcount == 1


class SqlIdempotency:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    @staticmethod
    def _lock_until(lock_seconds: int) -> Any:
        return sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, float(lock_seconds))

    async def begin(self, scope: str, key: str, request_hash: str, lock_seconds: int) -> Begin:
        t = idempotency_keys
        inserted = await self._c.execute(
            pg_insert(t)
            .values(
                scope=scope,
                key=key,
                request_hash=request_hash,
                locked_until=self._lock_until(lock_seconds),
            )
            .on_conflict_do_nothing()
        )
        if inserted.rowcount == 1:
            return New()
        row = (await self._c.execute(sa.select(t).where(t.c.scope == scope, t.c.key == key))).one()
        if row.request_hash != request_hash:
            return Mismatch()
        if row.status_code is not None:
            return Replay(row.status_code, row.response)
        # first attempt crashed without finishing: take over once its lock has expired
        took = await self._c.execute(
            sa.update(t)
            .where(
                t.c.scope == scope,
                t.c.key == key,
                t.c.status_code.is_(None),
                sa.or_(t.c.locked_until.is_(None), t.c.locked_until <= sa.func.now()),
            )
            .values(locked_until=self._lock_until(lock_seconds))
        )
        return New() if took.rowcount == 1 else InProgress()

    async def complete(self, scope: str, key: str, status: int, body: Any) -> None:
        t = idempotency_keys
        await self._c.execute(
            sa.update(t)
            .where(t.c.scope == scope, t.c.key == key)
            .values(status_code=status, response=body, locked_until=None)
        )

    async def release(self, scope: str, key: str) -> None:
        t = idempotency_keys
        await self._c.execute(
            sa.delete(t).where(t.c.scope == scope, t.c.key == key, t.c.status_code.is_(None))
        )


class SqlWebhookInbox:
    def __init__(self, conn: AsyncConnection, clock: Clock) -> None:
        self._c = conn
        self._clock = clock

    async def store(
        self, gateway: str, event_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> bool:
        now = self._clock.now()
        result = await self._c.execute(
            pg_insert(webhook_inbox)
            .values(
                gateway=gateway,
                event_id=event_id,
                event_type=event_type,
                payload=dict(payload),
                received_at=now,
                next_try_at=now,
            )
            .on_conflict_do_nothing(index_elements=["gateway", "event_id"])
        )
        return result.rowcount == 1

    async def claim_due(self, limit: int, lease_seconds: int, now: datetime) -> list[InboxRow]:
        t = webhook_inbox
        due = (
            sa.select(t.c.id)
            .where(t.c.status == "received", t.c.next_try_at <= now)
            .order_by(t.c.next_try_at, t.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._c.execute(
            sa.update(t)
            .where(t.c.id.in_(due))
            .values(next_try_at=now + timedelta(seconds=lease_seconds))
            .returning(
                t.c.id, t.c.event_id, t.c.event_type, t.c.payload, t.c.attempts, t.c.received_at
            )
        )
        rows = [
            InboxRow(r.id, r.event_id, r.event_type, r.payload, r.attempts, r.received_at)
            for r in result
        ]
        return sorted(rows, key=lambda r: r.id)

    async def mark(self, row_id: int, status: str) -> None:
        t = webhook_inbox
        await self._c.execute(sa.update(t).where(t.c.id == row_id).values(status=status))

    async def record_failure(
        self, row_id: int, error: str, now: datetime, max_attempts: int
    ) -> str:
        t = webhook_inbox
        attempts = (
            await self._c.execute(sa.select(t.c.attempts).where(t.c.id == row_id).with_for_update())
        ).scalar_one() + 1
        if attempts >= max_attempts:
            status, next_try = "failed", now
        else:
            status = "received"
            next_try = now + timedelta(seconds=min(30 * 2 ** (attempts - 1), 3600))
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == row_id)
            .values(attempts=attempts, status=status, next_try_at=next_try, last_error=error[:2000])
        )
        return status


class SqlJobs:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def enqueue(
        self, kind: str, payload: Mapping[str, Any], run_at: datetime, dedupe_key: str | None
    ) -> bool:
        result = await self._c.execute(
            pg_insert(jobs)
            .values(kind=kind, payload=dict(payload), run_at=run_at, dedupe_key=dedupe_key)
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
        return result.rowcount == 1

    async def claim(self, limit: int, lock_seconds: int, now: datetime) -> list[Job]:
        due = (
            sa.select(jobs.c.id)
            .where(jobs.c.status == "queued", jobs.c.run_at <= now)
            .order_by(jobs.c.run_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._c.execute(
            sa.update(jobs)
            .where(jobs.c.id.in_(due))
            .values(
                status="running",
                attempts=jobs.c.attempts + 1,
                locked_until=now + timedelta(seconds=lock_seconds),
            )
            .returning(jobs.c.id, jobs.c.kind, jobs.c.payload, jobs.c.attempts, jobs.c.max_attempts)
        )
        return [Job(r.id, r.kind, dict(r.payload), r.attempts, r.max_attempts) for r in result]

    async def complete(self, job_id: int) -> None:
        await self._c.execute(
            sa.update(jobs).where(jobs.c.id == job_id).values(status="done", locked_until=None)
        )

    async def fail(self, job_id: int, error: str, now: datetime) -> JobStatus:
        row = (
            await self._c.execute(
                sa.select(jobs.c.attempts, jobs.c.max_attempts)
                .where(jobs.c.id == job_id)
                .with_for_update()
            )
        ).one()
        if row.attempts >= row.max_attempts:
            status, run_at = JobStatus.DEAD, now
        else:
            status = JobStatus.QUEUED
            run_at = now + timedelta(seconds=min(2**row.attempts, 3600))
        await self._c.execute(
            sa.update(jobs)
            .where(jobs.c.id == job_id)
            .values(status=status.value, run_at=run_at, locked_until=None, last_error=error[:2000])
        )
        return status

    async def reap(self, now: datetime) -> int:
        result = await self._c.execute(
            sa.update(jobs)
            .where(jobs.c.status == "running", jobs.c.locked_until < now)
            .values(status="queued", locked_until=None)
        )
        return int(result.rowcount)


class SqlAudit:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def record(
        self,
        actor_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        reason: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
    ) -> None:
        await self._c.execute(
            sa.insert(admin_audit).values(
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                before=dict(before) if before is not None else None,
                after=dict(after) if after is not None else None,
            )
        )
