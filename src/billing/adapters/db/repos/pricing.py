"""Phase 3 implements these; the stubs keep the unit of work complete."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from billing.domain.catalog import ModelGroup
from billing.domain.rating import PriceBook


class SqlPricingRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def price_book_at(self, ts: datetime) -> PriceBook:
        raise NotImplementedError("Phase 3")

    async def fx_at(self, ts: datetime) -> Decimal:
        raise NotImplementedError("Phase 3")

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
        raise NotImplementedError("Phase 3")

    async def add_fx_rate(self, effective_from: date, usd_inr: Decimal, source: str) -> None:
        raise NotImplementedError("Phase 3")
