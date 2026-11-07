"""wallet_topup_requests: staff-initiated organisation top-ups."""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import wallet_topup_requests as t
from billing.app.records import TopupRequest
from billing.domain.money import Paise


def _record(r: Any) -> TopupRequest:
    return TopupRequest(
        id=r.id,
        billing_account_id=r.billing_account_id,
        amount_paise=Paise(r.amount_paise),
        status=r.status,
        gateway_link_id=r.gateway_link_id,
        payment_id=r.payment_id,
        created_by=r.created_by,
        reason=r.reason,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


class SqlTopupRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def insert(self, request: TopupRequest) -> None:
        await self._c.execute(
            sa.insert(t).values(
                id=request.id,
                billing_account_id=request.billing_account_id,
                amount_paise=request.amount_paise,
                status=request.status,
                gateway_link_id=request.gateway_link_id,
                created_by=request.created_by,
                reason=request.reason,
            )
        )

    async def get(self, request_id: UUID, *, lock: bool = False) -> TopupRequest | None:
        q = sa.select(t).where(t.c.id == request_id)
        r = (await self._c.execute(q.with_for_update() if lock else q)).first()
        return _record(r) if r else None

    async def by_link(self, link_id: str, *, lock: bool = False) -> TopupRequest | None:
        q = sa.select(t).where(t.c.gateway_link_id == link_id)
        r = (await self._c.execute(q.with_for_update() if lock else q)).first()
        return _record(r) if r else None

    async def set_link(self, request_id: UUID, link_id: str) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == request_id)
            .values(gateway_link_id=link_id, updated_at=sa.func.now())
        )

    async def mark_paid(self, request_id: UUID, payment_id: UUID) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == request_id)
            .values(status="paid", payment_id=payment_id, updated_at=sa.func.now())
        )

    async def cancel(self, request_id: UUID) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == request_id)
            .values(status="cancelled", updated_at=sa.func.now())
        )
