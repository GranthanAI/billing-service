"""Outbox, consumer inbox, idempotency keys, webhook inbox, jobs and admin audit."""

from collections.abc import Mapping
from datetime import datetime
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
from billing.app.records import Begin, InProgress, Mismatch, New, Replay


class SqlOutbox:
    """The Publisher port: writes in the caller's transaction."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def enqueue(
        self,
        topic: str,
        key: str,
        event_type: str,
        payload: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        await self._c.execute(
            sa.insert(outbox).values(
                topic=topic,
                key=key,
                event_type=event_type,
                payload=payload,
                headers=dict(headers or {}),
            )
        )


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


class SqlWebhookInbox:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def store(
        self, gateway: str, event_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> bool:
        result = await self._c.execute(
            pg_insert(webhook_inbox)
            .values(
                gateway=gateway, event_id=event_id, event_type=event_type, payload=dict(payload)
            )
            .on_conflict_do_nothing(index_elements=["gateway", "event_id"])
        )
        return result.rowcount == 1


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
