from datetime import date, datetime, time, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import usage_daily
from billing.adapters.db.tables import usage_events as t
from billing.app.records import UsageEventRecord
from billing.domain.calendar import IST
from billing.domain.catalog import ModelGroup


def _day_bounds(ist_day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(ist_day, time(0, 0), tzinfo=IST)
    return start, start + timedelta(days=1)


class SqlUsageRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def insert(self, record: UsageEventRecord) -> bool:
        result = await self._c.execute(
            pg_insert(t)
            .values(
                kind=record.kind,
                source_id=record.source_id,
                occurred_at=record.occurred_at,
                user_id=record.user_id,
                billing_account_id=record.billing_account_id,
                subscription_id=record.subscription_id,
                plan_version_id=record.plan_version_id,
                model=record.model,
                model_group=record.model_group.value if record.model_group else None,
                input_tokens=record.input_tokens,
                cached_input_tokens=record.cached_input_tokens,
                output_tokens=record.output_tokens,
                aux_input_tokens=record.aux_input_tokens,
                aux_output_tokens=record.aux_output_tokens,
                billable_units=record.billable_units,
                cost_micro_inr=record.cost_micro_inr,
                price_paise=record.price_paise,
                fx_usd_inr=record.fx_usd_inr,
                hold_id=record.hold_id,
                provider_costs=dict(record.provider_costs) if record.provider_costs else None,
                shortfall_taxable_paise=record.shortfall_taxable_paise,
            )
            .on_conflict_do_nothing(index_elements=["kind", "source_id", "occurred_at"])
        )
        return result.rowcount == 1

    async def counts_for_day(self, user_id: UUID, ist_day: date) -> dict[ModelGroup, int]:
        start, end = _day_bounds(ist_day)
        q = (
            sa.select(t.c.model_group, sa.func.count())
            .where(
                t.c.user_id == user_id,
                t.c.kind == "message",
                t.c.billable_units > 0,  # a failed attempt costs money but is not a message
                t.c.occurred_at >= start,
                t.c.occurred_at < end,
                t.c.model_group.is_not(None),
            )
            .group_by(t.c.model_group)
        )
        return {ModelGroup(g): n for g, n in (await self._c.execute(q)).all()}

    async def counts_by_user_for_day(self, ist_day: date) -> dict[UUID, dict[str, int]]:
        start, end = _day_bounds(ist_day)
        q = (
            sa.select(t.c.user_id, t.c.kind, t.c.model_group, sa.func.count())
            .where(
                t.c.kind.in_(("message", "manim_video")),
                t.c.billable_units > 0,
                t.c.occurred_at >= start,
                t.c.occurred_at < end,
            )
            .group_by(t.c.user_id, t.c.kind, t.c.model_group)
        )
        out: dict[UUID, dict[str, int]] = {}
        for user_id, kind, group, n in (await self._c.execute(q)).all():
            field = "manim" if kind == "manim_video" else group
            if field is None:
                continue
            counts = out.setdefault(user_id, {})
            counts[field] = counts.get(field, 0) + n
        return out

    async def youtube_counts_for_month(self, year: int, month: int) -> dict[UUID, int]:
        start = datetime.combine(date(year, month, 1), time(0, 0), tzinfo=IST)
        end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = datetime.combine(date(end_year, end_month, 1), time(0, 0), tzinfo=IST)
        q = (
            sa.select(t.c.user_id, sa.func.count())
            .where(
                t.c.kind == "youtube_video",
                t.c.billable_units > 0,
                t.c.occurred_at >= start,
                t.c.occurred_at < end,
            )
            .group_by(t.c.user_id)
        )
        return {user_id: n for user_id, n in (await self._c.execute(q)).all()}

    async def rollup_day(self, ist_day: date) -> int:
        """Upsert, not delete + insert: billing_app has no DELETE on usage_daily and the source
        rows are append-only, so recomputing the aggregate in place gives the same result."""
        start, end = _day_bounds(ist_day)
        agg = (
            sa.select(
                sa.literal(ist_day, type_=sa.Date),
                t.c.billing_account_id,
                t.c.user_id,
                t.c.model_group,
                sa.func.count().filter(t.c.billable_units > 0),  # messages that were answered
                sa.func.sum(t.c.billable_units),
                sa.func.sum(t.c.cost_micro_inr),
                sa.func.sum(t.c.price_paise),
            )
            .where(
                t.c.kind == "message",
                t.c.model_group.is_not(None),
                t.c.occurred_at >= start,
                t.c.occurred_at < end,
            )
            .group_by(t.c.billing_account_id, t.c.user_id, t.c.model_group)
        )
        ins = pg_insert(usage_daily).from_select(
            [
                "day",
                "billing_account_id",
                "user_id",
                "model_group",
                "messages",
                "billable_units",
                "cost_micro_inr",
                "price_paise",
            ],
            agg,
        )
        ex = ins.excluded
        ins = ins.on_conflict_do_update(
            index_elements=["day", "billing_account_id", "user_id", "model_group"],
            set_={
                "messages": ex.messages,
                "billable_units": ex.billable_units,
                "cost_micro_inr": ex.cost_micro_inr,
                "price_paise": ex.price_paise,
            },
        )
        return int((await self._c.execute(ins)).rowcount)
