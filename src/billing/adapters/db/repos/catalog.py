"""Catalog drafts and the published catalog (raw SQL, JSON-ready rows)."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

LIVE = "('active', 'past_due', 'suspended', 'cancelling')"


def _row(r: Any) -> dict[str, Any]:
    return dict(r._mapping)


class SqlCatalogRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def _all(self, query: str, **params: object) -> list[dict[str, Any]]:
        return [_row(r) for r in (await self._c.execute(sa.text(query), params)).all()]

    # --- the published catalog -------------------------------------------------------------

    async def get_active(self) -> dict[str, Any] | None:
        rows = await self._all(
            "SELECT catalog, catalog_version, draft_id, published_by, published_at "
            "FROM billing.active_catalog WHERE id = 1"
        )
        return rows[0] if rows else None

    async def set_active(
        self, catalog: dict[str, Any], version: str, draft_id: UUID, actor: UUID, at: datetime
    ) -> None:
        await self._c.execute(
            sa.text(
                "INSERT INTO billing.active_catalog "
                "(id, catalog, catalog_version, draft_id, published_by, published_at) "
                "VALUES (1, CAST(:c AS jsonb), :v, :d, :a, :t) "
                "ON CONFLICT (id) DO UPDATE SET catalog = EXCLUDED.catalog, "
                "catalog_version = EXCLUDED.catalog_version, draft_id = EXCLUDED.draft_id, "
                "published_by = EXCLUDED.published_by, published_at = EXCLUDED.published_at"
            ),
            {"c": json.dumps(catalog), "v": version, "d": draft_id, "a": actor, "t": at},
        )

    # --- drafts ----------------------------------------------------------------------------

    async def insert_draft(
        self,
        draft_id: UUID,
        based_on: str,
        catalog: dict[str, Any],
        note: str,
        diff: dict[str, Any],
        impact: dict[str, Any],
        warnings: list[str],
        actor: UUID,
    ) -> None:
        await self._c.execute(
            sa.text(
                "INSERT INTO billing.catalog_drafts "
                "(id, based_on_version, catalog, note, diff, impact, warnings, created_by) "
                "VALUES (:i, :b, CAST(:c AS jsonb), :n, CAST(:d AS jsonb), CAST(:m AS jsonb), "
                "CAST(:w AS jsonb), :a)"
            ),
            {
                "i": draft_id, "b": based_on, "c": json.dumps(catalog), "n": note,
                "d": json.dumps(diff), "m": json.dumps(impact), "w": json.dumps(warnings), "a": actor,
            },
        )  # fmt: skip

    async def get_draft(self, draft_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
        rows = await self._all(
            "SELECT * FROM billing.catalog_drafts WHERE id = :i" + (" FOR UPDATE" if lock else ""),
            i=draft_id,
        )
        return rows[0] if rows else None

    async def list_drafts(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT id, based_on_version, note, created_by, created_at, status, published_at, "
            "warnings FROM billing.catalog_drafts ORDER BY created_at DESC LIMIT :n",
            n=limit,
        )

    async def mark_published(self, draft_id: UUID, actor: UUID, reason: str, at: datetime) -> None:
        await self._c.execute(
            sa.text(
                "UPDATE billing.catalog_drafts SET status = 'published', published_by = :a, "
                "publish_reason = :r, published_at = :t WHERE id = :i"
            ),
            {"i": draft_id, "a": actor, "r": reason, "t": at},
        )

    # --- history and impact ----------------------------------------------------------------

    async def plan_version_history(self) -> list[dict[str, Any]]:
        return await self._all(
            f"""
            SELECT pv.plan_code, pv.variant, pv.version, pv.price_month_paise, pv.price_year_paise,
                   pv.limits, pv.effective_from AS created_at, pv.retired_at,
                   (SELECT count(*) FROM billing.subscriptions s
                     WHERE s.plan_version_id = pv.id AND s.status IN {LIVE}) AS active_subscribers
              FROM billing.plan_versions pv
             ORDER BY pv.plan_code, pv.variant NULLS FIRST, pv.version
            """  # noqa: S608
        )

    async def last_audit_id(self) -> int:
        rows = await self._all("SELECT coalesce(max(id), 0) AS n FROM billing.admin_audit")
        return int(rows[0]["n"])

    async def limits_only_audits_after(self, audit_id: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT id, reason, before, after FROM billing.admin_audit "
            "WHERE action = 'catalog.limits_only_change' AND id > :i ORDER BY id",
            i=audit_id,
        )
