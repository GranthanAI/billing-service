from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import plan_versions as t
from billing.app.records import PlanVersionRecord
from billing.domain.catalog import Limits, ModelGroup, OrgVariant, PlanVersion
from billing.domain.money import Paise

GROUP_PREFIX = "group:"


def encode_models(plan: PlanVersion) -> list[str]:
    """allowed_models holds `group:<name>` entries plus extra model ids as they are."""
    groups = sorted(f"{GROUP_PREFIX}{g.value}" for g in plan.allowed_groups)
    return [*groups, *sorted(plan.extra_models)]


def _record(r: Any) -> PlanVersionRecord:
    models: list[str] = list(r.allowed_models)
    plan = PlanVersion(
        id=r.id,
        plan_code=r.plan_code,
        version=r.version,
        rank=r.rank,
        variant=OrgVariant(r.variant) if r.variant else None,
        price_month=Paise(r.price_month_paise) if r.price_month_paise is not None else None,
        price_year=Paise(r.price_year_paise) if r.price_year_paise is not None else None,
        limits=Limits(**r.limits),
        allowed_groups=frozenset(
            ModelGroup(m.removeprefix(GROUP_PREFIX)) for m in models if m.startswith(GROUP_PREFIX)
        ),
        extra_models=frozenset(m for m in models if not m.startswith(GROUP_PREFIX)),
    )
    return PlanVersionRecord(
        plan=plan,
        effective_from=r.effective_from,
        checksum=r.checksum,
        razorpay_plan_month=r.razorpay_plan_month,
        razorpay_plan_year=r.razorpay_plan_year,
        retired_at=r.retired_at,
    )


class SqlPlanRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def by_id(self, plan_version_id: UUID) -> PlanVersionRecord | None:
        r = (await self._c.execute(sa.select(t).where(t.c.id == plan_version_id))).first()
        return _record(r) if r else None

    async def current(self, plan_code: str, variant: OrgVariant | None) -> PlanVersionRecord | None:
        v = variant.value if variant else None
        q = (
            sa.select(t)
            .where(
                t.c.plan_code == plan_code,
                t.c.variant.is_not_distinct_from(v),
                t.c.retired_at.is_(None),
            )
            .order_by(t.c.version.desc())
            .limit(1)
        )
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None

    async def all_current(self) -> Sequence[PlanVersionRecord]:
        q = sa.select(t).where(t.c.retired_at.is_(None)).order_by(t.c.plan_code, t.c.version)
        return [_record(r) for r in (await self._c.execute(q)).all()]

    async def insert_version(self, record: PlanVersionRecord) -> None:
        p = record.plan
        await self._c.execute(
            sa.insert(t).values(
                id=p.id,
                plan_code=p.plan_code,
                version=p.version,
                rank=p.rank,
                variant=p.variant.value if p.variant else None,
                price_month_paise=p.price_month,
                price_year_paise=p.price_year,
                limits=asdict(p.limits),
                allowed_models=encode_models(p),
                razorpay_plan_month=record.razorpay_plan_month,
                razorpay_plan_year=record.razorpay_plan_year,
                effective_from=record.effective_from,
                retired_at=record.retired_at,
                checksum=record.checksum,
            )
        )

    async def retire(self, plan_version_id: UUID, at: datetime) -> None:
        await self._c.execute(sa.update(t).where(t.c.id == plan_version_id).values(retired_at=at))

    async def set_gateway_plan_ids(
        self, plan_version_id: UUID, month: str | None, year: str | None
    ) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == plan_version_id)
            .values(razorpay_plan_month=month, razorpay_plan_year=year)
        )
