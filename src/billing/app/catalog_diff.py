"""What a draft changes compared with the active catalog: the diff, the warnings and the refusals (pure functions)."""

from typing import Any

from billing.app.catalog_spec import CatalogSpec, PlanBlock, blocks
from billing.domain.catalog import UNLIMITED

UPI_LIMIT_PAISE = 1_500_000  # UPI does not take single payments of Rs 15,000 or more
PRICE_WARNING = "A price change creates a new plan version; existing subscribers keep the old price until they change plan."
LIMIT_WARNING = "Lowering a limit needs 'Apply limits' for existing subscribers: they keep the old limits until you apply it."


def _rank(v: int) -> float:
    return float("inf") if v == UNLIMITED else float(v)


def _label(b: PlanBlock) -> str:
    return b.plan_code + (f"/{b.variant.value}" if b.variant else "")


def _block_diff(old: PlanBlock | None, new: PlanBlock) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "plan_code": new.plan_code,
        "variant": new.variant.value if new.variant else None,
    }
    if old is None:
        entry.update(change="added", prices=[], limits=[], groups={"added": sorted(g.value for g in new.groups), "removed": []},
                     extra_models={"added": sorted(new.extra_models), "removed": []})  # fmt: skip
        return entry
    prices = [
        {"interval": name, "from": a, "to": b}
        for name, a, b in (
            ("month", old.price_month, new.price_month),
            ("year", old.price_year, new.price_year),
        )
        if a != b
    ]
    limits = []
    for name in old.limits.__dataclass_fields__:
        a, b = getattr(old.limits, name), getattr(new.limits, name)
        if a != b:
            limits.append(
                {
                    "name": name,
                    "from": a,
                    "to": b,
                    "direction": "up" if _rank(b) > _rank(a) else "down",
                }
            )
    og, ng = {g.value for g in old.groups}, {g.value for g in new.groups}
    oe, ne = set(old.extra_models), set(new.extra_models)
    groups = {"added": sorted(ng - og), "removed": sorted(og - ng)}
    extra = {"added": sorted(ne - oe), "removed": sorted(oe - ne)}
    other = []
    if old.rank != new.rank:
        other.append({"name": "rank", "from": old.rank, "to": new.rank})
    if old.default_group != new.default_group:
        other.append(
            {
                "name": "default_group",
                "from": old.default_group.value,
                "to": new.default_group.value,
            }
        )
    if old.min_seats != new.min_seats:
        other.append({"name": "min_seats", "from": old.min_seats, "to": new.min_seats})
    changed = bool(
        prices
        or limits
        or groups["added"]
        or groups["removed"]
        or extra["added"]
        or extra["removed"]
        or other
    )
    entry.update(change="changed" if changed else "unchanged", prices=prices, limits=limits, groups=groups,
                 extra_models=extra, other=other)  # fmt: skip
    return entry


def _payg_diff(old: CatalogSpec, new: CatalogSpec) -> dict[str, Any]:
    o, n = old.payg, new.payg
    unit = [
        {"key": k, "from": o.unit_prices.get(k), "to": n.unit_prices.get(k)}
        for k in sorted(set(o.unit_prices) | set(n.unit_prices))
        if o.unit_prices.get(k) != n.unit_prices.get(k)
    ]
    op, np_ = dict(o.topup_packs), dict(n.topup_packs)
    packs = {
        "added": [{"code": c, "price": np_[c]} for c in np_ if c not in op],
        "removed": [{"code": c, "price": op[c]} for c in op if c not in np_],
        "changed": [
            {"code": c, "from": op[c], "to": np_[c]} for c in np_ if c in op and op[c] != np_[c]
        ],
    }
    other = [
        {"name": f, "from": getattr(o, f), "to": getattr(n, f)}
        for f in ("storage_over_10gb_per_gb_month", "long_message_tokens", "no_credit_fast_per_day",
                  "max_messages_per_minute", "org_min_topup", "credit_validity_months", "rank")
        if getattr(o, f) != getattr(n, f)
    ]  # fmt: skip
    return {"unit_prices": unit, "topup_packs": packs, "other": other}


def _models_diff(old: CatalogSpec, new: CatalogSpec) -> dict[str, Any]:
    og, ng = old.group_of(), new.group_of()
    return {
        "added": sorted(set(ng) - set(og)),
        "removed": sorted(set(og) - set(ng)),
        "moved": [{"id": m, "from": og[m].value, "to": ng[m].value} for m in sorted(og) if m in ng and og[m] != ng[m]],
        "relabelled": sorted(e.id for e in new.model_entries() if e.id in og and next(
            (x for x in old.model_entries() if x.id == e.id)).label != e.label),
    }  # fmt: skip


def diff_specs(old: CatalogSpec, new: CatalogSpec) -> dict[str, Any]:
    ob = {b.key: b for b in blocks(old)}
    plans = [_block_diff(ob.get(b.key), b) for b in blocks(new)]
    payg = _payg_diff(old, new)
    payg_changed = bool(payg["unit_prices"] or payg["other"] or any(payg["topup_packs"].values()))
    # the payg block's limits are part of its plan block diff; its prices/packs are reported here
    return {
        "plans": [p for p in plans if p["change"] != "unchanged"],
        "payg": payg,
        "models": _models_diff(old, new),
        "changed": bool(any(p["change"] != "unchanged" for p in plans) or payg_changed or any(_models_diff(old, new)[k] for k in ("added", "removed", "moved", "relabelled"))),
    }  # fmt: skip


def warnings_for(diff: dict[str, Any], new: CatalogSpec) -> list[str]:
    out: list[str] = []
    if any(p["prices"] for p in diff["plans"] if p["change"] == "changed"):
        out.append(PRICE_WARNING)
    if any(lim["direction"] == "down" for p in diff["plans"] for lim in p.get("limits", [])):
        out.append(LIMIT_WARNING)
    for b in blocks(new):
        if b.price_year is not None and b.price_year >= UPI_LIMIT_PAISE:
            out.append(
                f"{_label(b)}: the yearly price is {b.price_year / 100:,.0f} rupees; UPI is unavailable for single "
                "payments of Rs 15,000 or more, so buyers must use a card or e-mandate."
            )
    return out


def refusals(old: CatalogSpec, new: CatalogSpec, priced_models: frozenset[str]) -> list[str]:
    errors: list[str] = []
    old_codes = {p.code for p in old.plans} | {"payg"}
    new_codes = {p.code for p in new.plans} | {"payg"}
    for code in sorted(old_codes - new_codes):
        why = (
            "the free plan cannot be removed"
            if code == "free"
            else f"plan '{code}' cannot be removed or renamed"
        )
        errors.append(why)
    if new.catalog_version == old.catalog_version:
        errors.append(f"catalog_version must change (it is still '{old.catalog_version}')")
    for m in sorted(new.all_models() - priced_models):
        errors.append(f"model_groups: model '{m}' has no effective price in the price book")
    return errors


def describe_diff(diff: dict[str, Any]) -> list[str]:
    """The diff as readable lines (the CLI)."""
    out: list[str] = []
    for p in diff["plans"]:
        name = p["plan_code"] + (f"/{p['variant']}" if p["variant"] else "")
        if p["change"] == "added":
            out.append(f"plan {name}: added")
            continue
        out += [
            f"plan {name}: {x['interval']} price {x['from']} -> {x['to']} paise"
            for x in p["prices"]
        ]
        out += [
            f"plan {name}: limit {x['name']} {x['from']} -> {x['to']} ({x['direction']})"
            for x in p["limits"]
        ]
        out += [f"plan {name}: model group added {g}" for g in p["groups"]["added"]]
        out += [f"plan {name}: model group removed {g}" for g in p["groups"]["removed"]]
        out += [f"plan {name}: extra model added {m}" for m in p["extra_models"]["added"]]
        out += [f"plan {name}: extra model removed {m}" for m in p["extra_models"]["removed"]]
        out += [f"plan {name}: {x['name']} {x['from']} -> {x['to']}" for x in p.get("other", [])]
    payg = diff["payg"]
    out += [
        f"payg: unit price {x['key']} {x['from']} -> {x['to']} paise" for x in payg["unit_prices"]
    ]
    out += [
        f"payg: top-up pack added {x['code']} {x['price']}" for x in payg["topup_packs"]["added"]
    ]
    out += [f"payg: top-up pack removed {x['code']}" for x in payg["topup_packs"]["removed"]]
    out += [
        f"payg: top-up pack {x['code']} {x['from']} -> {x['to']}"
        for x in payg["topup_packs"]["changed"]
    ]
    out += [f"payg: {x['name']} {x['from']} -> {x['to']}" for x in payg["other"]]
    m = diff["models"]
    out += [f"model added {x}" for x in m["added"]] + [f"model removed {x}" for x in m["removed"]]
    out += [f"model {x['id']} moved {x['from']} -> {x['to']}" for x in m["moved"]]
    out += [f"model relabelled {x}" for x in m["relabelled"]]
    return out
