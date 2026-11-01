"""Tokens to cost (micro-rupees) and billable units. Decimal only inside, rounded once."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Literal

from billing.domain.catalog import ModelGroup, PaygPrices
from billing.domain.money import MicroInr, Paise

PURPOSES = frozenset({"answer", "request_analysis", "memory", "manim_plan"})


class UnknownModelPrice(Exception):
    pass


class NoFxRate(Exception):
    """No USD/INR rate is effective on or before the requested date."""


@dataclass(frozen=True, slots=True)
class LlmCall:
    provider: str
    model: str
    purpose: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            raise ValueError(f"unknown purpose: {self.purpose}")
        if min(self.input_tokens, self.cached_input_tokens, self.output_tokens) < 0:
            raise ValueError("token counts must be >= 0")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens cannot exceed input tokens")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_usd_per_m: Decimal
    cached_input_usd_per_m: Decimal | None
    output_usd_per_m: Decimal


class PriceBook:
    def __init__(self, prices: Mapping[str, ModelPrice]) -> None:
        self._prices: Mapping[str, ModelPrice] = MappingProxyType(dict(prices))

    def __contains__(self, model: object) -> bool:
        return model in self._prices

    def models(self) -> frozenset[str]:
        return frozenset(self._prices)

    def for_model(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            raise UnknownModelPrice(model) from None


@dataclass(frozen=True, slots=True)
class Rating:
    cost: MicroInr
    billable_units: int
    price: Paise


def rate(
    calls: Sequence[LlmCall],
    prices: PriceBook,
    usd_inr: Decimal,
    group: ModelGroup,
    mode: Literal["plan", "payg"],
    payg: PaygPrices,
) -> Rating:
    answers = [c for c in calls if c.purpose == "answer"]
    if len(answers) != 1:
        raise ValueError(f"exactly one answer call required, got {len(answers)}")

    cost_usd = Decimal(0)
    for c in calls:
        p = prices.for_model(c.model)
        uncached = c.input_tokens - c.cached_input_tokens
        cached_price = (
            p.cached_input_usd_per_m if p.cached_input_usd_per_m is not None else p.input_usd_per_m
        )
        cost_usd += (
            uncached * p.input_usd_per_m
            + c.cached_input_tokens * cached_price
            + c.output_tokens * p.output_usd_per_m
        ) / Decimal(1_000_000)
    cost = MicroInr(int((cost_usd * usd_inr * 1_000_000).to_integral_value(ROUND_HALF_UP)))

    if mode == "payg":
        units = max(1, -(-answers[0].input_tokens // payg.long_message_tokens))
        return Rating(cost, units, Paise(units * payg.unit_prices[group.value]))
    return Rating(cost, 1, Paise(0))
