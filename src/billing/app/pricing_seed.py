from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from billing.domain.catalog import ModelGroup


@dataclass(frozen=True, slots=True)
class ModelPriceSeed:
    model: str
    provider: str
    group: ModelGroup
    input_usd_per_m: Decimal
    cached_input_usd_per_m: Decimal | None
    output_usd_per_m: Decimal
    effective_from: datetime


@dataclass(frozen=True, slots=True)
class FxSeed:
    effective_from: date
    usd_inr: Decimal
    source: str
