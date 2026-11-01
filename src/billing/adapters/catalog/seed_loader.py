from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from billing.adapters.catalog.yaml_loader import CatalogError
from billing.app.pricing_seed import FxSeed, ModelPriceSeed
from billing.domain.catalog import ModelGroup


def _read(path: str | Path) -> Any:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc


def _dec(value: object, where: str) -> Decimal:
    # YAML floats like 0.075 go through str() so the Decimal is exactly what was written
    try:
        return Decimal(str(value))
    except Exception:
        raise CatalogError(f"{where}: not a number: {value!r}") from None


def load_model_prices(path: str | Path) -> list[ModelPriceSeed]:
    raw = _read(path)
    effective = raw["effective_from"]
    if not isinstance(effective, datetime) or effective.tzinfo is None:
        raise CatalogError(f"{path}: effective_from must be a timestamp with an offset")
    out: list[ModelPriceSeed] = []
    for m in raw["models"]:
        where = f"{path}: model '{m.get('model')}'"
        try:
            group = ModelGroup(m["group"])
        except ValueError:
            raise CatalogError(f"{where}: unknown group {m.get('group')!r}") from None
        cached = m.get("cached_input")
        out.append(
            ModelPriceSeed(
                model=m["model"],
                provider=m["provider"],
                group=group,
                input_usd_per_m=_dec(m["input"], where),
                cached_input_usd_per_m=_dec(cached, where) if cached is not None else None,
                output_usd_per_m=_dec(m["output"], where),
                effective_from=effective,
            )
        )
    return out


def load_fx_rates(path: str | Path) -> list[FxSeed]:
    raw = _read(path)
    out: list[FxSeed] = []
    for r in raw["rates"]:
        day = r["effective_from"]
        if not isinstance(day, date) or isinstance(day, datetime):
            raise CatalogError(f"{path}: effective_from must be a date")
        out.append(FxSeed(day, _dec(r["usd_inr"], str(path)), str(r["source"])))
    return out
