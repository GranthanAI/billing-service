"""Catalog sync: bring plan_versions in line with config/plans.yaml.

Versions are immutable. A changed block inserts version + 1 and retires the old row; the only
fields ever updated on a stored version are `retired_at` and the gateway plan ids.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from billing.app.catalog_spec import CatalogSpec, PlanBlock, blocks
from billing.app.ports import Clock, GatewayPlanSpec, IdGenerator, PlanGateway, UnitOfWork
from billing.app.records import PlanVersionRecord
from billing.domain.catalog import OrgVariant

SYSTEM_ACTOR = UUID(int=0)
LIMITS_ONLY_ACTION = "catalog.limits_only_change"


class CatalogSyncError(Exception):
    pass


class SyncAction(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    PRICE_CHANGE = "new version (price change)"
    LIMITS_ONLY = "new version (limits only)"
    RETIRED = "retired"


@dataclass(slots=True)
class PlanOutcome:
    plan_code: str
    variant: OrgVariant | None
    action: SyncAction
    version: int | None
    gateway_plans_created: int = 0

    @property
    def label(self) -> str:
        return self.plan_code + (f"/{self.variant.value}" if self.variant else "")

    def line(self) -> str:
        v = f" v{self.version}" if self.version is not None else ""
        gw = (
            f", gateway plans created: {self.gateway_plans_created}"
            if self.gateway_plans_created
            else ""
        )
        return f"{self.label}{v}: {self.action.value}{gw}"


@dataclass(slots=True)
class SyncReport:
    dry_run: bool
    outcomes: list[PlanOutcome] = field(default_factory=list)
    models_changed: int = 0

    @property
    def gateway_plans_created(self) -> int:
        return sum(o.gateway_plans_created for o in self.outcomes)

    @property
    def changed(self) -> bool:
        return (
            any(o.action is not SyncAction.UNCHANGED for o in self.outcomes)
            or self.gateway_plans_created > 0
            or self.models_changed > 0
        )

    def lines(self) -> list[str]:
        prefix = "[dry run] " if self.dry_run else ""
        return [prefix + o.line() for o in self.outcomes]


def _gateway_specs(block: PlanBlock, record: PlanVersionRecord) -> dict[str, GatewayPlanSpec]:
    """interval -> plan spec, only for the intervals this version has no gateway plan id for."""
    plan = record.plan
    out: dict[str, GatewayPlanSpec] = {}
    for interval, price, existing in (
        ("month", plan.price_month, record.razorpay_plan_month),
        ("year", plan.price_year, record.razorpay_plan_year),
    ):
        if price is None or existing is not None:
            continue
        period = "monthly" if interval == "month" else "yearly"
        out[interval] = GatewayPlanSpec(
            name=f"GranthanAI {block.plan_code.capitalize()} · {period}",
            period=period,
            interval=1,
            amount=price,
            currency="INR",
            notes={
                "plan_code": block.plan_code,
                "version": str(plan.version),
                "interval": interval,
                "checksum": record.checksum,
            },
        )
    return out


async def sync_catalog(
    spec: CatalogSpec,
    uow_factory: Callable[[], UnitOfWork],
    gateway: PlanGateway,
    clock: Clock,
    ids: IdGenerator,
    *,
    dry_run: bool = False,
) -> SyncReport:
    wanted = {b.key: b for b in blocks(spec)}
    report = SyncReport(dry_run=dry_run)
    now = clock.now()

    # --- read and check everything before writing anything -----------------------------------
    async with uow_factory() as uow:
        book = await uow.pricing.price_book_at(now)
        missing = sorted(spec.all_models() - book.models())
        if missing:
            raise CatalogSyncError(
                "no model price is effective now for: "
                + ", ".join(missing)
                + " (run `python -m billing.cli pricing seed` or add prices first)"
            )
        current = {(r.plan.plan_code, r.plan.variant): r for r in await uow.plans.all_current()}
        removed = [r for key, r in current.items() if key not in wanted]
        for r in removed:
            live = await uow.subscriptions.count_live_for_plan(r.plan.plan_code)
            if live:
                raise CatalogSyncError(
                    f"plan '{r.plan.plan_code}' was removed from plans.yaml but has {live} "
                    "live subscription(s); migrate them first"
                )

    # --- version changes, one transaction per plan --------------------------------------------
    for key, block in wanted.items():
        old = current.get(key)
        checksum = block.checksum()
        if old is not None and old.checksum == checksum:
            report.outcomes.append(
                PlanOutcome(*_ident(block), SyncAction.UNCHANGED, old.plan.version)
            )
            continue
        if old is None:
            action, version = SyncAction.CREATED, 1
        else:
            version = old.plan.version + 1
            same_prices = old.plan.price_month == block.price_month and (
                old.plan.price_year == block.price_year
            )
            action = SyncAction.LIMITS_ONLY if same_prices else SyncAction.PRICE_CHANGE
        report.outcomes.append(PlanOutcome(*_ident(block), action, version))
        if dry_run:
            continue
        new_id = ids.new()
        new = PlanVersionRecord(block.to_plan_version(new_id, version), now, checksum)
        async with uow_factory() as uow:
            await uow.plans.insert_version(new)
            if old is not None:
                await uow.plans.retire(old.plan.id, now)
            if action is SyncAction.LIMITS_ONLY and old is not None:
                await uow.audit.record(
                    SYSTEM_ACTOR,
                    LIMITS_ONLY_ACTION,
                    "plan_version",
                    str(new_id),
                    f"catalog sync: limits-only change to {block.plan_code}",
                    {"plan_version_id": str(old.plan.id), "limits": asdict(old.plan.limits)},
                    {"plan_version_id": str(new_id), "limits": asdict(block.limits)},
                )

    async with uow_factory() as uow:
        for r in removed:
            report.outcomes.append(
                PlanOutcome(r.plan.plan_code, r.plan.variant, SyncAction.RETIRED, r.plan.version)
            )
            if not dry_run:
                await uow.plans.retire(r.plan.id, now)

    # --- the model picker list: labels and order change without a new plan version -------------
    async with uow_factory() as uow:
        if dry_run:
            have = {e.id: e for e in await uow.plans.list_models()}
            want = spec.model_entries()
            report.models_changed = sum(have.get(e.id) != e for e in want) + len(
                set(have) - {e.id for e in want}
            )
        else:
            report.models_changed = await uow.plans.sync_models(spec.model_entries())

    # --- gateway plans: after the versions are committed; self-healing on every run ------------
    async with uow_factory() as uow:
        latest = await uow.plans.all_current()
    for rec in latest if not dry_run else _projected(current, wanted, report, now):
        gblock = wanted.get((rec.plan.plan_code, rec.plan.variant))
        if gblock is None or not gblock.sells_on_gateway:
            continue
        specs = _gateway_specs(gblock, rec)
        if not specs:
            continue
        outcome = next(o for o in report.outcomes if (o.plan_code, o.variant) == gblock.key)
        outcome.gateway_plans_created += len(specs)
        if dry_run:
            continue
        found: dict[str, str] = {}
        for interval, gspec in specs.items():
            found[interval] = await gateway.ensure_plan(gspec)
        async with uow_factory() as uow:
            await uow.plans.set_gateway_plan_ids(
                rec.plan.id,
                found.get("month", rec.razorpay_plan_month),
                found.get("year", rec.razorpay_plan_year),
            )
    return report


def _ident(block: PlanBlock) -> tuple[str, OrgVariant | None]:
    return (block.plan_code, block.variant)


def _projected(
    current: dict[tuple[str, OrgVariant | None], PlanVersionRecord],
    wanted: dict[tuple[str, OrgVariant | None], PlanBlock],
    report: SyncReport,
    now: datetime,
) -> list[PlanVersionRecord]:
    """Dry run: what the versions would look like, so gateway plans can be counted."""
    out: list[PlanVersionRecord] = []
    for key, block in wanted.items():
        outcome = next(o for o in report.outcomes if (o.plan_code, o.variant) == key)
        old = current.get(key)
        if outcome.action is SyncAction.UNCHANGED and old is not None:
            out.append(old)
        else:
            plan = block.to_plan_version(UUID(int=0), outcome.version or 1)
            out.append(PlanVersionRecord(plan, now, block.checksum()))
    return out
