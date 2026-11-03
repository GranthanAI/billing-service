from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import subscription_changes as t
from billing.app.records import ChangeKind, ChangeRecord, ChangeStatus
from billing.domain.catalog import Interval

OPEN = (ChangeStatus.PENDING.value, ChangeStatus.AUTHORISED.value)


def _record(r: Any) -> ChangeRecord:
    return ChangeRecord(
        id=r.id,
        subscription_id=r.subscription_id,
        kind=ChangeKind(r.kind),
        new_plan_version_id=r.new_plan_version_id,
        new_interval=Interval(r.new_interval),
        gateway_subscription_id=r.gateway_subscription_id,
        status=ChangeStatus(r.status),
        refund_payment_id=r.refund_payment_id,
        created_at=r.created_at,
    )


class SqlChangeRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def _one(self, q: sa.Select[Any]) -> ChangeRecord | None:
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None

    async def insert(self, record: ChangeRecord) -> None:
        extra: dict[str, Any] = {}
        if record.created_at is not None:
            extra = {"created_at": record.created_at, "updated_at": record.created_at}
        await self._c.execute(
            sa.insert(t).values(
                id=record.id,
                subscription_id=record.subscription_id,
                kind=record.kind.value,
                new_plan_version_id=record.new_plan_version_id,
                new_interval=record.new_interval.value,
                gateway_subscription_id=record.gateway_subscription_id,
                status=record.status.value,
                refund_payment_id=record.refund_payment_id,
                **extra,
            )
        )

    async def get(self, change_id: UUID) -> ChangeRecord | None:
        return await self._one(sa.select(t).where(t.c.id == change_id))

    async def by_gateway_id(self, gateway_subscription_id: str) -> ChangeRecord | None:
        return await self._one(
            sa.select(t).where(t.c.gateway_subscription_id == gateway_subscription_id)
        )

    async def open_for_subscription(self, subscription_id: UUID) -> ChangeRecord | None:
        return await self._one(
            sa.select(t).where(t.c.subscription_id == subscription_id, t.c.status.in_(OPEN))
        )

    async def set_status(self, change_id: UUID, status: ChangeStatus) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == change_id)
            .values(status=status.value, updated_at=sa.func.now())
        )

    async def list_pending_before(self, cutoff: datetime, limit: int) -> Sequence[ChangeRecord]:
        q = (
            sa.select(t)
            .where(t.c.status == ChangeStatus.PENDING.value, t.c.created_at < cutoff)
            .order_by(t.c.created_at)
            .limit(limit)
        )
        return [_record(r) for r in (await self._c.execute(q)).all()]
