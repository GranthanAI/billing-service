"""Read config/plans.yaml and validate it. Pydantic and PyYAML stay in this adapter."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from billing.app.catalog_spec import CatalogSpec, ModelEntry, PaygSpec, PlanSpec, VariantSpec
from billing.domain.catalog import UNLIMITED, Limits, ModelGroup, OrgVariant

LIMIT_FIELDS = tuple(f.name for f in fields(Limits))
PAYG_PRICE_KEYS = ("fast", "smart", "best", "ultra", "youtube_video", "manim_video")


class CatalogError(ValueError):
    """plans.yaml is invalid. The message names the plan and the field."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Models(_Strict):
    groups: list[str]
    extra: list[str] = []


class _Prices(_Strict):
    month: StrictInt | None = None
    year: StrictInt | None = None


class _Variant(_Strict):
    min_seats: StrictInt
    prices: _Prices
    limits: dict[str, Any]


class _ModelItem(_Strict):
    id: str
    label: str
    provider: str


class _Plan(_Strict):
    code: str
    rank: StrictInt
    default_group: str
    models: _Models
    prices: _Prices | None = None
    limits: dict[str, Any] | None = None
    org_only: StrictBool = False
    variants: dict[str, _Variant] | None = None


class _Payg(_Strict):
    code: str
    rank: StrictInt
    default_group: str
    models: _Models
    unit_prices: dict[str, StrictInt]
    storage_over_10gb_per_gb_month: StrictInt
    long_message_tokens: StrictInt
    no_credit_fast_per_day: StrictInt
    max_messages_per_minute: StrictInt
    topup_packs: list[dict[str, Any]]
    org_min_topup: StrictInt
    credit_validity_months: StrictInt


def _check(model: type[BaseModel], raw: object, where: str) -> Any:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise CatalogError(f"{where}: {loc or 'block'}: {first['msg']}") from exc


def _limits(raw: Mapping[str, Any], where: str) -> Limits:
    unknown = sorted(set(raw) - set(LIMIT_FIELDS))
    if unknown:
        raise CatalogError(f"{where}: unknown limit '{unknown[0]}'")
    values: dict[str, int] = {}
    for name in LIMIT_FIELDS:
        if name not in raw:
            raise CatalogError(f"{where}: limit '{name}' is missing")
        v = raw[name]
        if isinstance(v, bool) or not isinstance(v, int) or (v < 0 and v != UNLIMITED):
            raise CatalogError(f"{where}: limit '{name}' must be an int >= 0 or -1, got {v!r}")
        values[name] = v
    lim = Limits(**values)
    if (
        lim.best_per_day != UNLIMITED
        and lim.messages_per_day != UNLIMITED
        and lim.best_per_day > lim.messages_per_day
    ):
        raise CatalogError(
            f"{where}: best_per_day ({lim.best_per_day}) must not exceed "
            f"messages_per_day ({lim.messages_per_day})"
        )
    return lim


def _price(value: int | None, where: str, name: str) -> int | None:
    if value is not None and value < 0:
        raise CatalogError(f"{where}: prices.{name} must be >= 0 paise, got {value}")
    return value


def _groups(names: list[str], where: str, known: set[str]) -> tuple[ModelGroup, ...]:
    out: list[ModelGroup] = []
    for n in names:
        if n not in known:
            raise CatalogError(f"{where}: unknown model group '{n}'")
        out.append(ModelGroup(n))
    return tuple(out)


def parse_catalog(raw: object) -> CatalogSpec:
    if not isinstance(raw, dict):
        raise CatalogError("plans.yaml: top level must be a mapping")
    for key in (
        "catalog_version",
        "currency",
        "gst_inclusive",
        "model_groups",
        "base_limits",
        "plans",
    ):
        if key not in raw:
            raise CatalogError(f"plans.yaml: '{key}' is missing")

    # model groups: every model in exactly one group
    group_names = {g.value for g in ModelGroup}
    model_groups: dict[ModelGroup, tuple[ModelEntry, ...]] = {}
    seen: dict[str, str] = {}
    order = 0
    # picker order is the group order (fast, smart, best, ultra), then the order in the file
    for group in ModelGroup:
        models = raw["model_groups"].get(group.value, [])
        entries: list[ModelEntry] = []
        for item in models:
            m = _check(_ModelItem, item, f"model_groups.{group.value}")
            if m.id in seen:
                raise CatalogError(
                    f"model_groups: model '{m.id}' is in more than one group "
                    f"({seen[m.id]}, {group.value})"
                )
            if not m.label.strip() or not m.provider.strip():
                raise CatalogError(f"model_groups: model '{m.id}' needs a label and a provider")
            seen[m.id] = group.value
            entries.append(ModelEntry(m.id, m.label, m.provider, group, order))
            order += 1
        model_groups[group] = tuple(entries)
    for gname in raw["model_groups"]:
        if gname not in group_names:
            raise CatalogError(f"model_groups: unknown model group '{gname}'")

    base = _limits(raw["base_limits"], "base_limits")

    plans: list[PlanSpec] = []
    payg: PaygSpec | None = None
    codes: set[str] = set()
    for item in raw["plans"]:
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str):
            raise CatalogError("plans: every plan needs a 'code'")
        if code in codes:
            raise CatalogError(f"plan '{code}': duplicate plan code")
        codes.add(code)
        where = f"plan '{code}'"

        if code == "payg":
            p = _check(_Payg, item, where)
            groups = _groups(p.models.groups, where, group_names)
            _models_known(p.models.extra, seen, where)
            missing = [k for k in PAYG_PRICE_KEYS if k not in p.unit_prices]
            if missing:
                raise CatalogError(f"{where}: unit_prices is missing '{missing[0]}'")
            for k, v in p.unit_prices.items():
                if v < 0:
                    raise CatalogError(f"{where}: unit_prices.{k} must be >= 0 paise")
            if p.long_message_tokens <= 0:
                raise CatalogError(f"{where}: long_message_tokens must be > 0")
            payg = PaygSpec(
                rank=p.rank,
                default_group=_default_group(p.default_group, groups, where),
                groups=groups,
                unit_prices=dict(p.unit_prices),
                long_message_tokens=p.long_message_tokens,
                no_credit_fast_per_day=p.no_credit_fast_per_day,
                storage_over_10gb_per_gb_month=p.storage_over_10gb_per_gb_month,
                max_messages_per_minute=p.max_messages_per_minute,
                topup_packs=tuple((str(t["code"]), int(t["price"])) for t in p.topup_packs),
                org_min_topup=p.org_min_topup,
                credit_validity_months=p.credit_validity_months,
            )
            continue

        p = _check(_Plan, item, where)
        groups = _groups(p.models.groups, where, group_names)
        _models_known(p.models.extra, seen, where)
        default = _default_group(p.default_group, groups, where)
        if p.variants is not None:
            plans.append(_org_plan(p, groups, default, where))
        else:
            plans.append(_individual_plan(p, groups, default, where))

    if payg is None:
        raise CatalogError("plans: the 'payg' plan is missing")
    _unique_ranks(plans)

    return CatalogSpec(
        catalog_version=str(raw["catalog_version"]),
        currency=str(raw["currency"]),
        gst_inclusive=bool(raw["gst_inclusive"]),
        model_groups=model_groups,
        base_limits=base,
        plans=tuple(plans),
        payg=payg,
    )


def _default_group(name: str, allowed: tuple[ModelGroup, ...], where: str) -> ModelGroup:
    try:
        group = ModelGroup(name)
    except ValueError:
        raise CatalogError(f"{where}: default_group: unknown model group '{name}'") from None
    if group not in allowed:
        raise CatalogError(f"{where}: default_group '{name}' is not one of the plan's model groups")
    if group in (ModelGroup.BEST, ModelGroup.ULTRA):
        raise CatalogError(f"{where}: default_group must not be '{name}' (Auto never spends it)")
    return group


def _models_known(extra: list[str], seen: Mapping[str, str], where: str) -> None:
    for m in extra:
        if m not in seen:
            raise CatalogError(f"{where}: extra model '{m}' is not in any model group")


def _individual_plan(
    p: _Plan, groups: tuple[ModelGroup, ...], default: ModelGroup, where: str
) -> PlanSpec:
    if p.limits is None:
        raise CatalogError(f"{where}: limits is missing")
    limits = _limits(p.limits, where)
    month = year = None
    if p.code == "free":
        if p.prices is not None and (p.prices.month is not None or p.prices.year is not None):
            raise CatalogError(f"{where}: the free plan must not have prices")
    elif p.org_only:
        raise CatalogError(f"{where}: org_only plans need variants")
    else:
        if p.prices is None or p.prices.month is None:
            raise CatalogError(f"{where}: prices.month is required")
        if p.prices.year is None:
            raise CatalogError(f"{where}: prices.year is required")
        month = _price(p.prices.month, where, "month")
        year = _price(p.prices.year, where, "year")
    return PlanSpec(
        code=p.code,
        rank=p.rank,
        groups=groups,
        extra_models=tuple(p.models.extra),
        price_month=month,
        price_year=year,
        limits=limits,
        default_group=default,
    )


def _org_plan(
    p: _Plan, groups: tuple[ModelGroup, ...], default: ModelGroup, where: str
) -> PlanSpec:
    assert p.variants is not None
    if p.prices is not None or p.limits is not None:
        raise CatalogError(f"{where}: organisation plans carry prices and limits per variant")
    variants: list[VariantSpec] = []
    for vname, v in p.variants.items():
        vwhere = f"{where} variant '{vname}'"
        try:
            variant = OrgVariant(vname)
        except ValueError:
            raise CatalogError(f"{vwhere}: unknown variant (expected college or company)") from None
        if v.min_seats < 1:
            raise CatalogError(f"{vwhere}: min_seats must be >= 1, got {v.min_seats}")
        if v.prices.month is not None:
            raise CatalogError(
                f"{vwhere}: prices.month is not allowed (organisation plans are yearly)"
            )
        if v.prices.year is None:
            raise CatalogError(f"{vwhere}: prices.year is required")
        year = _price(v.prices.year, vwhere, "year")
        assert year is not None
        variants.append(VariantSpec(variant, v.min_seats, year, _limits(v.limits, vwhere)))
    return PlanSpec(
        code=p.code,
        rank=p.rank,
        groups=groups,
        extra_models=tuple(p.models.extra),
        default_group=default,
        org_only=True,
        variants=tuple(variants),
    )


def _unique_ranks(plans: list[PlanSpec]) -> None:
    """Ranks are unique within an audience: individual, or one organisation variant."""
    audiences: dict[str, dict[int, str]] = defaultdict(dict)
    for p in plans:
        keys = [f"organisation {v.variant.value}" for v in p.variants] or ["individual"]
        for audience in keys:
            other = audiences[audience].get(p.rank)
            if other is not None:
                raise CatalogError(
                    f"plan '{p.code}': rank {p.rank} is already used by plan '{other}' "
                    f"in the {audience} audience"
                )
            audiences[audience][p.rank] = p.code


def load_catalog(path: str | Path) -> CatalogSpec:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    return parse_catalog(raw)
