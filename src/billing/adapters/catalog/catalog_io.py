"""CatalogSpec <-> plain dict (the plans.yaml schema) <-> YAML text. Used by the admin catalog API and the CLI.

`spec_to_raw` is the inverse of `parse_catalog`: parsing its output gives back an equal CatalogSpec, so an export
round-trips and a draft stored as JSON is validated by exactly the loader that reads plans.yaml.
"""

from dataclasses import asdict
from typing import Any

import yaml

from billing.app.catalog_spec import CatalogSpec, PaygSpec, PlanSpec
from billing.domain.catalog import Limits


def _limits(lim: Limits) -> dict[str, int]:
    return {k: int(v) for k, v in asdict(lim).items()}


def _plan(p: PlanSpec) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": p.code,
        "rank": p.rank,
        "default_group": p.default_group.value,
    }
    if p.default_model:
        out["default_model"] = p.default_model
    if p.org_only:
        out["org_only"] = True
    models: dict[str, Any] = {"groups": [g.value for g in p.groups]}
    if p.extra_models:
        models["extra"] = list(p.extra_models)
    out["models"] = models
    if p.variants:
        out["variants"] = {
            v.variant.value: {
                "min_seats": v.min_seats,
                "prices": {"year": v.price_year},
                "limits": _limits(v.limits),
            }
            for v in p.variants
        }
        return out
    prices = {k: v for k, v in (("month", p.price_month), ("year", p.price_year)) if v is not None}
    if prices:
        out["prices"] = prices
    assert p.limits is not None
    out["limits"] = _limits(p.limits)
    return out


def _payg(p: PaygSpec) -> dict[str, Any]:
    return {
        "code": "payg",
        "rank": p.rank,
        "default_group": p.default_group.value,
        "models": {"groups": [g.value for g in p.groups]},
        "unit_prices": dict(p.unit_prices),
        "storage_over_10gb_per_gb_month": p.storage_over_10gb_per_gb_month,
        "long_message_tokens": p.long_message_tokens,
        "no_credit_fast_per_day": p.no_credit_fast_per_day,
        "max_messages_per_minute": p.max_messages_per_minute,
        "topup_packs": [{"code": c, "price": price} for c, price in p.topup_packs],
        "org_min_topup": p.org_min_topup,
        "credit_validity_months": p.credit_validity_months,
        "limits": _limits(p.limits),
    }


def spec_to_raw(spec: CatalogSpec) -> dict[str, Any]:
    """The catalog as the plans.yaml structure (no anchors or comments; JSON-safe)."""
    return {
        "catalog_version": spec.catalog_version,
        "currency": spec.currency,
        "gst_inclusive": spec.gst_inclusive,
        "model_groups": {
            g.value: [
                {"id": e.id, "label": e.label, "provider": e.provider, "think": e.think} for e in entries
            ]
            for g, entries in spec.model_groups.items()
        },
        "base_limits": _limits(spec.base_limits),
        "plans": [*(_plan(p) for p in spec.plans), _payg(spec.payg)],
    }


def payg_public(spec: CatalogSpec) -> dict[str, object]:
    """What the pricing page shows for Pay as you Go (paise): unit prices, top-up packs, validity, storage price."""
    p = spec.payg
    return {
        "unit_prices": dict(p.unit_prices),
        "topup_packs": [{"code": c, "price_paise": price} for c, price in p.topup_packs],
        "credit_validity_months": p.credit_validity_months,
        "storage_over_10gb_per_gb_month": p.storage_over_10gb_per_gb_month,
        "long_message_tokens": p.long_message_tokens,
        "no_credit_fast_per_day": p.no_credit_fast_per_day,
        "max_messages_per_minute": p.max_messages_per_minute,
    }


def raw_to_yaml(raw: dict[str, Any]) -> str:
    header = "# Exported from the active billing catalog. Commit it as config/plans.yaml to make it the seed.\n"
    return header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=120)
