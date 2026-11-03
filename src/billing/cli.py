"""Operator commands: `python -m billing.cli catalog sync` and `pricing seed`.

They connect as the migrator role (BILLING_MIGRATION_DATABASE_URL), because only the migrator
may write plan_versions and the pricing tables.
"""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
from billing.adapters.catalog.yaml_loader import CatalogError, load_catalog
from billing.adapters.clock import SystemClock
from billing.adapters.db.engine import make_engine
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.gateways import make_gateway
from billing.adapters.ids import Uuid7Generator
from billing.adapters.valkey.client import make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.app.catalog import CatalogSyncError, sync_catalog
from billing.app.ports import PlanGateway
from billing.domain.calendar import ist_day
from billing.domain.rating import NoFxRate


class CliSettings(BaseSettings):
    """Only what the CLI needs, so it does not require the API's secrets."""

    model_config = SettingsConfigDict(extra="ignore")

    billing_migration_database_url: str
    payment_gateway: str = "razorpay"
    environment: str = "development"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""


def _gateway(settings: CliSettings) -> PlanGateway:
    if settings.payment_gateway == "razorpay" and not (
        settings.razorpay_key_id and settings.razorpay_key_secret
    ):
        raise ValueError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required")
    return make_gateway(
        settings.payment_gateway,
        settings.environment,
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        webhook_secret=settings.razorpay_webhook_secret,
        clock=SystemClock(),
    )


async def _catalog_sync(path: Path, dry_run: bool) -> int:
    spec = load_catalog(path)  # validate before touching the database
    settings = CliSettings()  # type: ignore[call-arg]
    gateway = _gateway(settings)
    engine = make_engine(settings.billing_migration_database_url, pool_size=2)
    ids = Uuid7Generator()
    try:
        report = await sync_catalog(
            spec, lambda: SqlUnitOfWork(engine, ids), gateway, SystemClock(), ids, dry_run=dry_run
        )
    finally:
        await engine.dispose()
    for line in report.lines():
        print(line)
    print(f"gateway plans created: {report.gateway_plans_created}")
    print(f"models changed: {report.models_changed}")
    return 0


class RebuildSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    billing_database_url: str
    billing_valkey_url: str
    day_counter_ttl_seconds: int = 172_800
    month_counter_ttl_seconds: int = 40 * 86_400


async def _valkey_rebuild(day: date | None) -> int:
    """Rewrite one IST day's counters (and that month's YouTube counts) from usage_events.

    Open holds are not rebuilt: they last at most 10 minutes and are lost with the instance.
    """
    settings = RebuildSettings()  # type: ignore[call-arg]
    target = day or ist_day(SystemClock().now())
    engine = make_engine(settings.billing_database_url, pool_size=2)
    client = make_valkey(settings.billing_valkey_url)
    counters = ValkeyCounters(
        client,
        day_ttl=settings.day_counter_ttl_seconds,
        month_ttl=settings.month_counter_ttl_seconds,
    )
    try:
        async with SqlUnitOfWork(engine, Uuid7Generator()) as uow:
            per_day = await uow.usage.counts_by_user_for_day(target)
            per_month = await uow.usage.youtube_counts_for_month(target.year, target.month)
        for user_id, counts in per_day.items():
            await counters.rebuild_day(user_id, target, counts)
        for user_id, videos in per_month.items():
            await counters.rebuild_month(user_id, target.year, target.month, {"youtube": videos})
    finally:
        await client.aclose()
        await engine.dispose()
    users = len(set(per_day) | set(per_month))
    print(f"rebuilt counters for {users} user(s) for {target}")
    return 0


async def _pricing_seed(prices: Path, fx: Path) -> int:
    price_rows = load_model_prices(prices)
    fx_rows = load_fx_rates(fx)
    settings = CliSettings()  # type: ignore[call-arg]
    engine = make_engine(settings.billing_migration_database_url, pool_size=2)
    try:
        async with SqlUnitOfWork(engine, Uuid7Generator()) as uow:
            for p in price_rows:
                await uow.pricing.add_model_price(
                    model=p.model,
                    provider=p.provider,
                    model_group=p.group,
                    input_usd_per_m=p.input_usd_per_m,
                    cached_input_usd_per_m=p.cached_input_usd_per_m,
                    output_usd_per_m=p.output_usd_per_m,
                    effective_from=p.effective_from,
                )
            for r in fx_rows:
                await uow.pricing.add_fx_rate(r.effective_from, r.usd_inr, r.source)
    finally:
        await engine.dispose()
    print(f"seeded {len(price_rows)} model prices and {len(fx_rows)} fx rate(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billing")
    top = parser.add_subparsers(dest="group", required=True)

    catalog = top.add_parser("catalog").add_subparsers(dest="cmd", required=True)
    sync = catalog.add_parser("sync", help="sync config/plans.yaml into plan_versions")
    sync.add_argument("--file", default="config/plans.yaml", type=Path)
    sync.add_argument("--dry-run", action="store_true")

    pricing = top.add_parser("pricing").add_subparsers(dest="cmd", required=True)
    seed = pricing.add_parser("seed", help="load model prices and FX rates (idempotent)")
    seed.add_argument("--prices", default="config/seed/model_prices.yaml", type=Path)
    seed.add_argument("--fx", default="config/seed/fx_rates.yaml", type=Path)

    valkey = top.add_parser("valkey").add_subparsers(dest="cmd", required=True)
    rebuild = valkey.add_parser("rebuild", help="rebuild one day's counters from usage_events")
    rebuild.add_argument("--day", type=date.fromisoformat, default=None, help="IST day, YYYY-MM-DD")

    args = parser.parse_args(argv)
    try:
        if args.group == "valkey":
            return asyncio.run(_valkey_rebuild(args.day))
        if args.group == "catalog":
            return asyncio.run(_catalog_sync(args.file, args.dry_run))
        return asyncio.run(_pricing_seed(args.prices, args.fx))
    except (CatalogError, CatalogSyncError, NoFxRate, ValueError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
