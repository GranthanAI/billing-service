"""Plan catalog types. Loading from plans.yaml is Phase 3."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from uuid import UUID

from billing.domain.money import Paise

UNLIMITED = -1


class ModelGroup(StrEnum):
    FAST = "fast"
    SMART = "smart"
    BEST = "best"
    ULTRA = "ultra"


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


class OwnerType(StrEnum):
    USER = "user"
    ORG = "org"


class OrgVariant(StrEnum):
    COLLEGE = "college"
    COMPANY = "company"


class ReserveKind(StrEnum):
    MESSAGE = "message"
    YOUTUBE_VIDEO = "youtube_video"
    MANIM_VIDEO = "manim_video"


class DenyReason(StrEnum):
    DAILY_LIMIT = "daily_limit"
    MONTHLY_LIMIT = "monthly_limit"
    MODEL_NOT_IN_PLAN = "model_not_in_plan"
    NO_CREDIT = "no_credit"
    SPEND_CAP = "spend_cap"
    MEMBER_SPEND_CAP = "member_spend_cap"
    ACCOUNT_BLOCKED = "account_blocked"


class PlanNotSoldForInterval(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Limits:
    messages_per_day: int
    best_per_day: int
    branches_per_tree: int
    messages_per_conversation: int
    memory_facts: int
    context_tokens: int
    documents_per_tree: int
    file_size_mb: int
    youtube_per_month: int
    storage_mb: int
    custom_modes: int
    mode_instruction_chars: int

    def __post_init__(self) -> None:
        for f in fields(self):
            v: int = getattr(self, f.name)
            if v < UNLIMITED:
                raise ValueError(f"{f.name} must be >= 0 or -1, got {v}")

    def _get(self, field: str) -> int:
        if field not in {f.name for f in fields(self)}:
            raise ValueError(f"unknown limit: {field}")
        value: int = getattr(self, field)
        return value

    def allows(self, field: str, used: int) -> bool:
        limit = self._get(field)
        return limit == UNLIMITED or used < limit

    def remaining(self, field: str, used: int) -> int:
        limit = self._get(field)
        return UNLIMITED if limit == UNLIMITED else max(0, limit - used)


@dataclass(frozen=True, slots=True)
class PlanVersion:
    id: UUID
    plan_code: str
    version: int
    rank: int
    variant: OrgVariant | None
    price_month: Paise | None
    price_year: Paise | None
    limits: Limits
    allowed_groups: frozenset[ModelGroup]
    extra_models: frozenset[str] = frozenset()
    default_group: ModelGroup = ModelGroup.FAST  # what an Auto request reserves
    min_seats: int | None = None  # organisation plans only

    def price_for(self, interval: Interval) -> Paise:
        price = self.price_month if interval is Interval.MONTH else self.price_year
        if price is None:
            raise PlanNotSoldForInterval(f"{self.plan_code} is not sold per {interval.value}")
        return price

    def allows_model(self, model: str, group_of: Mapping[str, ModelGroup]) -> bool:
        if model in self.extra_models:
            return True
        group = group_of.get(model)
        return group is not None and group in self.allowed_groups


@dataclass(frozen=True, slots=True)
class PaygPrices:
    unit_prices: Mapping[str, Paise]
    long_message_tokens: int = 32000
    no_credit_fast_per_day: int = 10
