from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import refunds as t
from billing.app.records import RefundRecord
from billing.domain.money import Paise


def _record(r: Any) -> RefundRecord:
    return RefundRecord(
        id=r.id,
        payment_id=r.payment_id,
        gateway_refund_id=r.gateway_refund_id,
        amount_paise=Paise(r.amount_paise),
        reason=r.reason,
        status=r.status,
    )


class SqlRefundRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def insert(self, record: RefundRecord) -> bool:
        result = await self._c.execute(
            pg_insert(t)
            .values(
                id=record.id,
                payment_id=record.payment_id,
                gateway_refund_id=record.gateway_refund_id,
                amount_paise=record.amount_paise,
                reason=record.reason,
                status=record.status,
            )
            .on_conflict_do_nothing(index_elements=["gateway_refund_id"])
        )
        return result.rowcount == 1

    async def by_gateway_id(self, gateway_refund_id: str) -> RefundRecord | None:
        r = (
            await self._c.execute(sa.select(t).where(t.c.gateway_refund_id == gateway_refund_id))
        ).first()
        return _record(r) if r else None

    async def set_status(self, gateway_refund_id: str, status: str) -> bool:
        result = await self._c.execute(
            sa.update(t).where(t.c.gateway_refund_id == gateway_refund_id).values(status=status)
        )
        return result.rowcount == 1

    async def total_for_payment(self, payment_id: UUID) -> Paise:
        q = sa.select(sa.func.coalesce(sa.func.sum(t.c.amount_paise), 0)).where(
            t.c.payment_id == payment_id, t.c.status != "failed"
        )
        return Paise(int((await self._c.execute(q)).scalar_one()))
