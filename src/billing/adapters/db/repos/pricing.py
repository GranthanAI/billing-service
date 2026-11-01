from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import fx_rates, model_prices
from billing.domain.calendar import ist_day
from billing.domain.catalog import ModelGroup
from billing.domain.rating import ModelPrice, NoFxRate, PriceBook


class SqlPricingRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def price_book_at(self, ts: datetime) -> PriceBook:
        """For each model, the row with the greatest effective_from at or before `ts`."""
        t = model_prices
        q = (
            sa.select(t)
            .distinct(t.c.model)
            .where(t.c.effective_from <= ts)
            .order_by(t.c.model, t.c.effective_from.desc())
        )
        return PriceBook(
            {
                r.model: ModelPrice(r.input_usd_per_m, r.cached_input_usd_per_m, r.output_usd_per_m)
                for r in (await self._c.execute(q)).all()
            }
        )

    async def model_groups_at(self, ts: datetime) -> dict[str, ModelGroup]:
        t = model_prices
        q = (
            sa.select(t.c.model, t.c.model_group)
            .distinct(t.c.model)
            .where(t.c.effective_from <= ts)
            .order_by(t.c.model, t.c.effective_from.desc())
        )
        return {r.model: ModelGroup(r.model_group) for r in (await self._c.execute(q)).all()}

    async def fx_at(self, ts: datetime) -> Decimal:
        """The latest rate effective on or before the IST calendar date of `ts`."""
        t = fx_rates
        q = (
            sa.select(t.c.usd_inr)
            .where(t.c.effective_from <= ist_day(ts))
            .order_by(t.c.effective_from.desc())
            .limit(1)
        )
        rate = (await self._c.execute(q)).scalar_one_or_none()
        if rate is None:
            raise NoFxRate(f"no USD/INR rate on or before {ist_day(ts)}")
        return Decimal(rate)

    async def add_model_price(
        self,
        *,
        model: str,
        provider: str,
        model_group: ModelGroup,
        input_usd_per_m: Decimal,
        cached_input_usd_per_m: Decimal | None,
        output_usd_per_m: Decimal,
        effective_from: datetime,
    ) -> None:
        """Idempotent on (model, effective_from): the seed can be re-run safely."""
        await self._c.execute(
            pg_insert(model_prices)
            .values(
                model=model,
                provider=provider,
                model_group=model_group.value,
                input_usd_per_m=input_usd_per_m,
                cached_input_usd_per_m=cached_input_usd_per_m,
                output_usd_per_m=output_usd_per_m,
                effective_from=effective_from,
            )
            .on_conflict_do_nothing(index_elements=["model", "effective_from"])
        )

    async def add_fx_rate(self, effective_from: date, usd_inr: Decimal, source: str) -> None:
        await self._c.execute(
            pg_insert(fx_rates)
            .values(effective_from=effective_from, usd_inr=usd_inr, source=source)
            .on_conflict_do_nothing(index_elements=["effective_from"])
        )
