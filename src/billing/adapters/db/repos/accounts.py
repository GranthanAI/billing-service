from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import billing_accounts as t
from billing.app.ports import IdGenerator
from billing.app.records import BillingAccount
from billing.domain.catalog import OwnerType


def _account(r: Any) -> BillingAccount:
    return BillingAccount(
        id=r.id,
        owner_type=OwnerType(r.owner_type),
        owner_id=r.owner_id,
        billing_email=r.billing_email,
        legal_name=r.legal_name,
        gstin=r.gstin,
        state_code=r.state_code,
        gateway_customer_id=r.gateway_customer_id,
        blocked_at=r.blocked_at,
        blocked_reason=r.blocked_reason,
    )


class SqlAccountRepo:
    def __init__(self, conn: AsyncConnection, ids: IdGenerator) -> None:
        self._c = conn
        self._ids = ids

    async def get_by_owner(self, owner_type: OwnerType, owner_id: UUID) -> BillingAccount | None:
        r = (
            await self._c.execute(
                sa.select(t).where(t.c.owner_type == owner_type.value, t.c.owner_id == owner_id)
            )
        ).first()
        return _account(r) if r else None

    async def get_or_create(
        self, owner_type: OwnerType, owner_id: UUID, email: str | None = None
    ) -> BillingAccount:
        await self._c.execute(
            pg_insert(t)
            .values(
                id=self._ids.new(),
                owner_type=owner_type.value,
                owner_id=owner_id,
                billing_email=email,
            )
            .on_conflict_do_nothing(index_elements=["owner_type", "owner_id"])
        )
        found = await self.get_by_owner(owner_type, owner_id)
        assert found is not None
        return found

    async def get(self, account_id: UUID) -> BillingAccount | None:
        r = (await self._c.execute(sa.select(t).where(t.c.id == account_id))).first()
        return _account(r) if r else None

    async def update_checkout(
        self,
        account_id: UUID,
        *,
        state_code: str,
        email: str,
        legal_name: str | None,
        gstin: str | None,
    ) -> None:
        values: dict[str, Any] = {"state_code": state_code, "billing_email": email}
        if legal_name is not None:
            values["legal_name"] = legal_name
        if gstin is not None:
            values["gstin"] = gstin
        await self._c.execute(sa.update(t).where(t.c.id == account_id).values(**values))

    async def set_blocked(self, account_id: UUID, reason: str | None, now: datetime) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == account_id)
            .values(
                blocked_at=now if reason is not None else None,
                blocked_reason=reason,
            )
        )

    async def anonymise(self, account_id: UUID) -> None:
        """Drop what identifies the person; the state code and GSTIN stay as tax records."""
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == account_id)
            .values(billing_email=None, legal_name=None, gateway_customer_id=None)
        )

    async def set_state_code(self, account_id: UUID, code: str) -> None:
        await self._c.execute(sa.update(t).where(t.c.id == account_id).values(state_code=code))

    async def set_gstin(self, account_id: UUID, gstin: str | None, legal_name: str | None) -> None:
        await self._c.execute(
            sa.update(t).where(t.c.id == account_id).values(gstin=gstin, legal_name=legal_name)
        )
