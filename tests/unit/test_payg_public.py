"""The pricing page's Pay as you Go numbers come from the catalog (paise), not from the frontend."""

from pathlib import Path

from billing.adapters.catalog.catalog_io import payg_public
from billing.adapters.catalog.yaml_loader import load_catalog
from billing.api.rest.public import _plan_json
from billing.app.catalog_spec import blocks

ROOT = Path(__file__).resolve().parents[2]


def test_payg_public_has_unit_prices_packs_and_validity() -> None:
    info = payg_public(load_catalog(ROOT / "config" / "plans.yaml"))
    assert info["unit_prices"]["youtube_video"] == 100 and info["unit_prices"]["manim_video"] == 200  # type: ignore[index]
    assert info["storage_over_10gb_per_gb_month"] == 1000
    assert info["topup_packs"][0] == {"code": "ind_199", "price_paise": 19900}  # type: ignore[index]
    assert info["credit_validity_months"] == 12 and info["max_messages_per_minute"] == 30


def test_only_the_payg_plan_json_carries_it() -> None:
    from billing.app.records import PlanVersionRecord
    from uuid import uuid4

    spec = load_catalog(ROOT / "config" / "plans.yaml")
    info = payg_public(spec)
    by = {b.plan_code: b for b in blocks(spec) if b.variant is None}
    rec = lambda code: PlanVersionRecord(by[code].to_plan_version(uuid4(), 1), None, "x")  # type: ignore[arg-type]  # noqa: E731
    assert _plan_json(rec("payg"), info)["payg"] == info
    assert "payg" not in _plan_json(rec("pro"), info)
