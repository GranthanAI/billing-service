"""The price rows the integration databases are seeded with.

config/seed/model_prices.yaml carries rows that start later than the test clock (a group move on 2026-11-19, a new model on
2026-11-21, a price change on 2027-01-01). The tests run with the clock in early November, so each model is seeded with the
latest row that starts before 2026-12-01 dated at the base day, and rows from 2027 on keep their own date (they are price
changes in the future)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from billing.adapters.catalog.seed_loader import load_model_prices

IST = timezone(timedelta(hours=5, minutes=30))
BASE = datetime(2026, 10, 25, tzinfo=IST)
CUTOFF = datetime(2026, 12, 1, tzinfo=IST)


def seed_prices(root: Path):  # type: ignore[no-untyped-def]
    rows = load_model_prices(root / "config" / "seed" / "model_prices.yaml")
    latest = {}
    for p in sorted((r for r in rows if r.effective_from < CUTOFF), key=lambda r: r.effective_from):
        latest[p.model] = p
    return [replace(p, effective_from=BASE) for p in latest.values()] + [r for r in rows if r.effective_from >= CUTOFF]
