"""The admin catalog API's logic: the active catalog, drafts (validated by the plans.yaml loader), their diff and impact,
and publishing (catalog sync, audit, entitlements.changed). A published catalog is stored in the database and wins over
config/plans.yaml, which stays the seed."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from billing.app.admin import Staff, need_reason
from billing.app.catalog import CatalogSyncError, sync_catalog
from billing.app.catalog_diff import diff_specs, refusals, warnings_for
from billing.app.catalog_spec import CatalogSpec
from billing.app.entitlement_events import announce_owner
from billing.app.errors import CatalogConflict, CatalogInvalid, NotFound
from billing.app.ports import Clock, IdGenerator, PlanGateway, UnitOfWork
from billing.domain.catalog import OwnerType

Parse = Callable[[dict[str, Any]], CatalogSpec]
ToRaw = Callable[[CatalogSpec], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ActiveCatalog:
    spec: CatalogSpec
    source: str  # "yaml" | "published draft"
    published_at: datetime | None = None
    draft_id: UUID | None = None


def _lowered(new: int, old: int) -> bool:
    """True when the limit got stricter (-1 is unlimited)."""
    if new == old or new == -1:
        return False
    return old == -1 or new < old


def _invalidator(
    fn: Callable[[UUID], Awaitable[None]], owner_id: UUID
) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        await fn(owner_id)

    return run


class CatalogAdminService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        ids: IdGenerator,
        gateway: PlanGateway,
        *,
        parse: Parse,
        to_raw: ToRaw,
        seed: Callable[[], CatalogSpec],
        parse_error: type[Exception],
        invalidate: Callable[[UUID], Awaitable[None]],
    ) -> None:
        self._uow, self._clock, self._ids, self._gateway = uow_factory, clock, ids, gateway
        self._parse, self._to_raw, self._seed = parse, to_raw, seed
        self._parse_error = parse_error
        self._invalidate = invalidate

    # --- the active catalog -------------------------------------------------------------------

    async def _active(self, uow: UnitOfWork) -> ActiveCatalog:
        row = await uow.catalog.get_active()
        if row is None:
            return ActiveCatalog(self._seed(), "yaml")
        return ActiveCatalog(
            self._parse(row["catalog"]), "published draft", row["published_at"], row["draft_id"]
        )

    async def active(self) -> ActiveCatalog:
        async with self._uow() as uow:
            return await self._active(uow)

    async def get_catalog(self) -> dict[str, Any]:
        async with self._uow() as uow:
            active = await self._active(uow)
            history = await uow.catalog.plan_version_history()
        return {
            "catalog": self._to_raw(active.spec),
            "catalog_version": active.spec.catalog_version,
            "source": active.source,
            "published_at": active.published_at,
            "plan_versions": history,
        }

    async def export_raw(self) -> dict[str, Any]:
        return self._to_raw((await self.active()).spec)

    # --- drafts -------------------------------------------------------------------------------

    def _validate(
        self, raw: dict[str, Any], old: CatalogSpec, priced: frozenset[str]
    ) -> CatalogSpec:
        try:
            new = self._parse(raw)  # the SAME rules as plans.yaml
        except self._parse_error as exc:
            raise CatalogInvalid([str(exc)]) from None
        problems = refusals(old, new, priced)
        if problems:
            raise CatalogInvalid(problems)
        return new

    async def create_draft(self, staff: Staff, raw: dict[str, Any], note: str) -> dict[str, Any]:
        now = self._clock.now()
        async with self._uow() as uow:
            active = await self._active(uow)
            priced = frozenset((await uow.pricing.price_book_at(now)).models())
            new = self._validate(raw, active.spec, priced)
            diff = diff_specs(active.spec, new)
            impact = await self._impact(uow, diff)
            warnings = warnings_for(diff, new)
            draft_id = self._ids.new()
            await uow.catalog.insert_draft(
                draft_id,
                active.spec.catalog_version,
                self._to_raw(new),
                note.strip(),
                diff,
                impact,
                warnings,
                staff.id,
            )
        return {
            "draft_id": str(draft_id),
            "based_on_version": active.spec.catalog_version,
            "note": note.strip(),
            "diff": diff,
            "impact": impact,
            "warnings": warnings,
        }

    async def _impact(self, uow: UnitOfWork, diff: dict[str, Any]) -> dict[str, Any]:
        current = {
            (r.plan.plan_code, r.plan.variant.value if r.plan.variant else None): r.plan.version
            for r in await uow.plans.all_current()
        }
        rows = []
        for p in diff["plans"]:
            if p["change"] != "changed":
                continue
            rows.append(
                {
                    "plan_code": p["plan_code"],
                    "variant": p["variant"],
                    "current_version": current.get((p["plan_code"], p["variant"])),
                    "active_subscribers": await uow.subscriptions.count_live_for_plan(
                        p["plan_code"]
                    ),
                }
            )
        return {"plans": rows, "total_subscribers": sum(r["active_subscribers"] for r in rows)}

    async def list_drafts(self) -> list[dict[str, Any]]:
        async with self._uow() as uow:
            return await uow.catalog.list_drafts()

    async def get_draft(self, draft_id: UUID) -> dict[str, Any]:
        async with self._uow() as uow:
            draft = await uow.catalog.get_draft(draft_id)
        if draft is None:
            raise NotFound("draft not found")
        return draft

    # --- publish ------------------------------------------------------------------------------

    async def publish(self, staff: Staff, draft_id: UUID, reason: str) -> dict[str, Any]:
        reason = need_reason(reason)
        started = self._clock.now()
        async with self._uow() as uow:
            draft = await uow.catalog.get_draft(draft_id, lock=True)
            if draft is None:
                raise NotFound("draft not found")
            if draft["status"] != "open":
                raise CatalogConflict("this draft was already published")
            active = await self._active(uow)
            if draft["based_on_version"] != active.spec.catalog_version:
                raise CatalogConflict(
                    f"the catalog changed to '{active.spec.catalog_version}' since this draft was made "
                    f"(it was based on '{draft['based_on_version']}'): reload and re-apply your changes"
                )
            priced = frozenset((await uow.pricing.price_book_at(started)).models())
            new = self._validate(draft["catalog"], active.spec, priced)
            audit_mark = await uow.catalog.last_audit_id()

        try:  # the same sync as `catalog sync`, in its own transactions (gateway plans are created here)
            report = await sync_catalog(new, self._uow, self._gateway, self._clock, self._ids)
        except CatalogSyncError as exc:
            raise CatalogInvalid([str(exc)]) from None

        applied: list[dict[str, Any]] = []
        needs_apply: list[dict[str, Any]] = []
        async with self._uow() as uow:
            now = self._clock.now()
            await uow.catalog.set_active(
                self._to_raw(new), new.catalog_version, draft_id, staff.id, now
            )
            await uow.catalog.mark_published(draft_id, staff.id, reason, now)
            for row in await uow.catalog.limits_only_audits_after(audit_mark):
                before, after = row["before"], row["after"]
                old_limits = before.get("limits", {})
                fell = [k for k, v in after["limits"].items() if _lowered(v, old_limits.get(k, v))]
                entry = {
                    "audit_id": row["id"],
                    "plan_code": after.get("plan_code"),
                    "limits_lowered": fell,
                }
                if (
                    fell
                ):  # existing subscribers keep their old limits until someone applies the change
                    needs_apply.append(entry)
                    continue
                owners = await uow.admin.repoint_plan_version(
                    UUID(before["plan_version_id"]), UUID(after["plan_version_id"])
                )
                for owner in owners:  # the limits only rose: everyone gets them now
                    await announce_owner(
                        uow, OwnerType(owner["owner_type"]), owner["owner_id"], "catalog", now
                    )
                    if owner["owner_type"] == OwnerType.USER.value:
                        uow.after_commit(_invalidator(self._invalidate, owner["owner_id"]))
                applied.append({**entry, "subscriptions_moved": len(owners)})
            await uow.audit.record(
                staff.id,
                "catalog.publish",
                "catalog",
                new.catalog_version,
                reason,
                {"catalog_version": active.spec.catalog_version, "source": active.source},
                {
                    "catalog_version": new.catalog_version,
                    "draft_id": str(draft_id),
                    "sync": report.lines(),
                },
            )
        return {
            "catalog_version": new.catalog_version,
            "draft_id": str(draft_id),
            "sync": report.lines(),
            "limits_raised_applied": applied,
            "apply_limits": needs_apply,  # POST /catalog/apply-limits/{audit_id} for each
        }
