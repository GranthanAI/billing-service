from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import payments as t
from billing.app.records import PaymentRecord
from billing.domain.money import Paise


def _record(r: Any) -> PaymentRecord:
    return PaymentRecord(
        id=r.id,
        billing_account_id=r.billing_account_id,
        purpose=r.purpose,
        gateway_payment_id=r.gateway_payment_id,
        amount_paise=Paise(r.amount_paise),
        status=r.status,
        captured_at=r.captured_at,
        raw=r.raw,
        subscription_id=r.subscription_id,
        gateway_order_id=r.gateway_order_id,
        method=r.method,
        fee_paise=Paise(r.fee_paise) if r.fee_paise is not None else None,
        fee_gst_paise=Paise(r.fee_gst_paise) if r.fee_gst_paise is not None else None,
    )


class SqlPaymentRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def insert(self, record: PaymentRecord) -> bool:
        result = await self._c.execute(
            pg_insert(t)
            .values(
                id=record.id,
                billing_account_id=record.billing_account_id,
                subscription_id=record.subscription_id,
                purpose=record.purpose,
                gateway_payment_id=record.gateway_payment_id,
                gateway_order_id=record.gateway_order_id,
                method=record.method,
                amount_paise=record.amount_paise,
                fee_paise=record.fee_paise,
                fee_gst_paise=record.fee_gst_paise,
                status=record.status,
                captured_at=record.captured_at,
                raw=dict(record.raw),
            )
            .on_conflict_do_nothing(index_elements=["gateway_payment_id"])
        )
        return result.rowcount == 1

    async def by_gateway_id(self, gateway_payment_id: str) -> PaymentRecord | None:
        r = (
            await self._c.execute(sa.select(t).where(t.c.gateway_payment_id == gateway_payment_id))
        ).first()
        return _record(r) if r else None

    async def mark_refunded(self, payment_id: UUID, total_refunded: Paise) -> None:
        """`refunded` once the whole amount is back, `partially_refunded` before that."""
        status = sa.case(
            (sa.literal(int(total_refunded)) >= t.c.amount_paise, "refunded"),
            else_="partially_refunded",
        )
        await self._c.execute(sa.update(t).where(t.c.id == payment_id).values(status=status))
