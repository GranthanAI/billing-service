from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import plan_overrides as t
from billing.app.records import OverrideRecord
from billing.domain.catalog import OwnerType


class SqlOverrideRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def active_for(
        self, owner_type: OwnerType, owner_id: UUID, now: datetime
    ) -> Sequence[OverrideRecord]:
        q = sa.select(t).where(
            t.c.owner_type == owner_type.value,
            t.c.owner_id == owner_id,
            t.c.ended_at.is_(None),
            t.c.starts_at <= now,
            t.c.expires_at > now,
        )
        return [
            OverrideRecord(
                id=r.id,
                owner_type=OwnerType(r.owner_type),
                owner_id=r.owner_id,
                plan_version_id=r.plan_version_id,
                reason=r.reason,
                starts_at=r.starts_at,
                expires_at=r.expires_at,
                granted_by=r.granted_by,
                ended_at=r.ended_at,
            )
            for r in (await self._c.execute(q)).all()
        ]

    async def insert(self, record: OverrideRecord) -> None:
        await self._c.execute(
            sa.insert(t).values(
                id=record.id,
                owner_type=record.owner_type.value,
                owner_id=record.owner_id,
                plan_version_id=record.plan_version_id,
                reason=record.reason,
                starts_at=record.starts_at,
                expires_at=record.expires_at,
                ended_at=record.ended_at,
                granted_by=record.granted_by,
            )
        )

    async def end(self, override_id: UUID, now: datetime) -> None:
        await self._c.execute(sa.update(t).where(t.c.id == override_id).values(ended_at=now))
