"""The parsed plan catalog as plain data, and the per-version blocks that sync works on."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from uuid import UUID

from billing.domain.catalog import Limits, ModelGroup, OrgVariant, PlanVersion
from billing.domain.money import Paise


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One model in the picker: id, label, provider and its place in the list."""

    id: str
    label: str
    provider: str
    group: ModelGroup
    sort_order: int


@dataclass(frozen=True, slots=True)
class VariantSpec:
    variant: OrgVariant
    min_seats: int
    price_year: int
    limits: Limits


@dataclass(frozen=True, slots=True)
class PlanSpec:
    code: str
    rank: int
    groups: tuple[ModelGroup, ...]
    extra_models: tuple[str, ...] = ()
    price_month: int | None = None
    price_year: int | None = None
    limits: Limits | None = None  # None for plans that only have variants
    default_group: ModelGroup = ModelGroup.FAST
    org_only: bool = False
    variants: tuple[VariantSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class PaygSpec:
    rank: int
    default_group: ModelGroup
    groups: tuple[ModelGroup, ...]
    unit_prices: Mapping[str, int]
    long_message_tokens: int
    no_credit_fast_per_day: int
    storage_over_10gb_per_gb_month: int
    max_messages_per_minute: int
    topup_packs: tuple[tuple[str, int], ...]
    org_min_topup: int
    credit_validity_months: int


@dataclass(frozen=True, slots=True)
class CatalogSpec:
    catalog_version: str
    currency: str
    gst_inclusive: bool
    model_groups: Mapping[ModelGroup, tuple[ModelEntry, ...]]
    base_limits: Limits
    plans: tuple[PlanSpec, ...]
    payg: PaygSpec

    def group_of(self) -> dict[str, ModelGroup]:
        return {e.id: g for g, entries in self.model_groups.items() for e in entries}

    def all_models(self) -> frozenset[str]:
        return frozenset(e.id for entries in self.model_groups.values() for e in entries)

    def model_entries(self) -> list[ModelEntry]:
        return sorted(
            (e for es in self.model_groups.values() for e in es), key=lambda e: e.sort_order
        )


@dataclass(frozen=True, slots=True)
class PlanBlock:
    """One (plan_code, variant): what becomes one plan_versions row."""

    plan_code: str
    variant: OrgVariant | None
    rank: int
    price_month: int | None
    price_year: int | None
    limits: Limits
    groups: tuple[ModelGroup, ...]
    extra_models: tuple[str, ...]
    default_group: ModelGroup = ModelGroup.FAST
    min_seats: int | None = None
    payg: PaygSpec | None = None

    @property
    def key(self) -> tuple[str, OrgVariant | None]:
        return (self.plan_code, self.variant)

    @property
    def sells_on_gateway(self) -> bool:
        """Individual paid plans only: not free, payg or organisation plans."""
        return self.variant is None and self.price_month is not None and self.payg is None

    def normalised(self) -> dict[str, object]:
        """Everything that defines the version, after merges. No comments, no gateway ids."""
        body: dict[str, object] = {
            "plan_code": self.plan_code,
            "variant": self.variant.value if self.variant else None,
            "rank": self.rank,
            "prices": {"month": self.price_month, "year": self.price_year},
            "limits": asdict(self.limits),
            "groups": sorted(g.value for g in self.groups),
            "extra_models": sorted(self.extra_models),
            "default_group": self.default_group.value,
            "min_seats": self.min_seats,
        }
        if self.payg is not None:
            payg = asdict(self.payg)
            payg["groups"] = sorted(g.value for g in self.payg.groups)
            payg["topup_packs"] = [list(p) for p in self.payg.topup_packs]
            body["payg"] = payg
        return body

    def checksum(self) -> str:
        canonical = json.dumps(self.normalised(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def prices(self) -> tuple[int | None, int | None]:
        return (self.price_month, self.price_year)

    def to_plan_version(self, plan_id: UUID, version: int) -> PlanVersion:
        return PlanVersion(
            id=plan_id,
            plan_code=self.plan_code,
            version=version,
            rank=self.rank,
            variant=self.variant,
            price_month=Paise(self.price_month) if self.price_month is not None else None,
            price_year=Paise(self.price_year) if self.price_year is not None else None,
            limits=self.limits,
            allowed_groups=frozenset(self.groups),
            extra_models=frozenset(self.extra_models),
            default_group=self.default_group,
            min_seats=self.min_seats,
        )


def blocks(spec: CatalogSpec) -> list[PlanBlock]:
    """Flatten the catalog into one block per (plan_code, variant)."""
    out: list[PlanBlock] = []
    for p in spec.plans:
        if p.variants:
            for v in p.variants:
                out.append(
                    PlanBlock(
                        p.code,
                        v.variant,
                        p.rank,
                        None,
                        v.price_year,
                        v.limits,
                        p.groups,
                        p.extra_models,
                        default_group=p.default_group,
                        min_seats=v.min_seats,
                    )
                )
        else:
            assert p.limits is not None
            out.append(
                PlanBlock(
                    p.code,
                    None,
                    p.rank,
                    p.price_month,
                    p.price_year,
                    p.limits,
                    p.groups,
                    p.extra_models,
                    default_group=p.default_group,
                )
            )
    out.append(
        PlanBlock(
            "payg",
            None,
            spec.payg.rank,
            None,
            None,
            spec.base_limits,
            spec.payg.groups,
            (),
            default_group=spec.payg.default_group,
            payg=spec.payg,
        )
    )
    return out
