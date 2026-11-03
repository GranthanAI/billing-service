from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import plan_versions, subscription_history
from billing.adapters.db.tables import subscriptions as t
from billing.app.records import StaleRecord, SubscriptionHistoryEntry, SubscriptionRecord
from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.subscription import SubscriptionState, SubStatus

LIVE = ("active", "past_due", "suspended", "cancelling")


def _record(r: Any) -> SubscriptionRecord:
    state = SubscriptionState(
        status=SubStatus(r.status),
        plan_version_id=r.plan_version_id,
        interval=Interval(r.interval),
        current_period_start=r.current_period_start,
        current_period_end=r.current_period_end,
        grace_until=r.grace_until,
        pending_plan_version_id=r.pending_plan_version_id,
        pending_interval=Interval(r.pending_interval) if r.pending_interval else None,
        gateway_subscription_id=r.gateway_subscription_id,
        next_gateway_subscription_id=r.next_gateway_subscription_id,
        last_period_paid=Paise(r.last_period_paid_paise)
        if r.last_period_paid_paise is not None
        else None,
    )
    return SubscriptionRecord(
        id=r.id,
        billing_account_id=r.billing_account_id,
        state=state,
        seats=r.seats,
        gateway=r.gateway,
        lock_version=r.lock_version,
        created_at=r.created_at,
    )


def _values(rec: SubscriptionRecord) -> dict[str, Any]:
    s = rec.state
    return {
        "billing_account_id": rec.billing_account_id,
        "plan_version_id": s.plan_version_id,
        "interval": s.interval.value,
        "seats": rec.seats,
        "status": s.status.value,
        "gateway": rec.gateway,
        "gateway_subscription_id": s.gateway_subscription_id,
        "next_gateway_subscription_id": s.next_gateway_subscription_id,
        "current_period_start": s.current_period_start,
        "current_period_end": s.current_period_end,
        "grace_until": s.grace_until,
        "pending_plan_version_id": s.pending_plan_version_id,
        "pending_interval": s.pending_interval.value if s.pending_interval else None,
        "last_period_paid_paise": s.last_period_paid,
    }


class SqlSubscriptionRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def _one(self, q: sa.Select[Any]) -> SubscriptionRecord | None:
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None

    async def get(self, subscription_id: UUID) -> SubscriptionRecord | None:
        return await self._one(sa.select(t).where(t.c.id == subscription_id))

    async def lock(self, subscription_id: UUID) -> SubscriptionRecord | None:
        return await self._one(sa.select(t).where(t.c.id == subscription_id).with_for_update())

    async def lock_by_gateway_id(self, gateway_subscription_id: str) -> SubscriptionRecord | None:
        q = (
            sa.select(t)
            .where(
                sa.or_(
                    t.c.gateway_subscription_id == gateway_subscription_id,
                    t.c.next_gateway_subscription_id == gateway_subscription_id,
                )
            )
            .with_for_update()
        )
        return await self._one(q)

    async def live_for_account(self, billing_account_id: UUID) -> SubscriptionRecord | None:
        q = sa.select(t).where(t.c.billing_account_id == billing_account_id, t.c.status.in_(LIVE))
        return await self._one(q)

    async def find_reusable_incomplete(
        self,
        billing_account_id: UUID,
        plan_version_id: UUID,
        interval: Interval,
        created_after: datetime,
    ) -> SubscriptionRecord | None:
        q = (
            sa.select(t)
            .where(
                t.c.billing_account_id == billing_account_id,
                t.c.status == SubStatus.INCOMPLETE.value,
                t.c.plan_version_id == plan_version_id,
                t.c.interval == interval.value,
                t.c.created_at > created_after,
            )
            .order_by(t.c.created_at.desc())
            .limit(1)
        )
        return await self._one(q)

    async def list_incomplete_before(
        self, cutoff: datetime, limit: int
    ) -> Sequence[SubscriptionRecord]:
        q = (
            sa.select(t)
            .where(t.c.status == SubStatus.INCOMPLETE.value, t.c.created_at < cutoff)
            .order_by(t.c.created_at)
            .limit(limit)
        )
        return [_record(r) for r in (await self._c.execute(q)).all()]

    async def count_live_for_plan(self, plan_code: str) -> int:
        q = (
            sa.select(sa.func.count())
            .select_from(t.join(plan_versions, plan_versions.c.id == t.c.plan_version_id))
            .where(plan_versions.c.plan_code == plan_code, t.c.status.in_(LIVE))
        )
        return int((await self._c.execute(q)).scalar_one())

    async def insert(self, record: SubscriptionRecord) -> None:
        extra: dict[str, Any] = {}
        if record.created_at is not None:
            extra = {"created_at": record.created_at, "updated_at": record.created_at}
        await self._c.execute(
            sa.insert(t).values(
                id=record.id, lock_version=record.lock_version, **_values(record), **extra
            )
        )

    async def save(self, record: SubscriptionRecord) -> SubscriptionRecord:
        result = await self._c.execute(
            sa.update(t)
            .where(t.c.id == record.id, t.c.lock_version == record.lock_version)
            .values(
                lock_version=record.lock_version + 1, updated_at=sa.func.now(), **_values(record)
            )
        )
        if result.rowcount != 1:
            raise StaleRecord(f"subscription {record.id} changed since it was read")
        return replace(record, lock_version=record.lock_version + 1)

    async def append_history(self, entry: SubscriptionHistoryEntry) -> None:
        await self._c.execute(
            sa.insert(subscription_history).values(
                subscription_id=entry.subscription_id,
                from_status=entry.from_status.value if entry.from_status else None,
                to_status=entry.to_status.value,
                trigger=entry.trigger,
                cause_ref=entry.cause_ref,
            )
        )
