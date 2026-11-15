"""B4d: the admin catalog API's logic (drafts validated by the plans.yaml loader, diff and impact, publish)."""

import copy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
import yaml
from fastapi import FastAPI

from billing.adapters.catalog.catalog_io import raw_to_yaml, spec_to_raw
from billing.adapters.catalog.yaml_loader import CatalogError, parse_catalog
from billing.adapters.ids import Uuid7Generator
from billing.api.rest import admin as admin_routes
from billing.api.rest.errors import install as install_errors
from billing.app.admin import Staff
from billing.app.catalog import sync_catalog
from billing.app.catalog_admin import CatalogAdminService
from billing.app.errors import CatalogConflict, CatalogInvalid, Forbidden
from billing.app.records import SubscriptionRecord
from billing.domain.catalog import Interval, OwnerType
from billing.domain.subscription import SubscriptionState, SubStatus
from tests.integration.conftest import FreshDb
from tests.integration.test_catalog import ROOT, TEXT, seed
from tests.support.clock import TestClock
from tests.support.fake_gateway import FakeGateway

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
ADMIN = Staff(uuid4(), "billing_admin")
SUPPORT = Staff(uuid4(), "support")


def base_raw() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(TEXT)


def draft_raw(**edits: object) -> dict:  # type: ignore[type-arg]
    """The seed catalog with a new catalog_version and Pro: 65 messages a day, 599 rupees a month."""
    raw = copy.deepcopy(base_raw())
    raw["catalog_version"] = "2026-12-01"
    pro = next(p for p in raw["plans"] if p["code"] == "pro")
    pro["limits"] = {**pro["limits"], "messages_per_day": 65}
    pro["prices"] = {"month": 59900, "year": 499000}
    raw.update(edits)
    return raw


async def make(
    db: FreshDb, gw: FakeGateway | None = None
) -> tuple[CatalogAdminService, FakeGateway]:
    await seed(db)
    gw = gw or FakeGateway()
    clock = TestClock(NOW)
    ids = Uuid7Generator()
    seed_spec = parse_catalog(base_raw())
    await sync_catalog(seed_spec, db.migrator_uow, gw, clock, ids)

    async def invalidate(_owner: UUID) -> None:
        return None

    svc = CatalogAdminService(
        db.app_uow, clock, ids, gw, parse=parse_catalog, to_raw=spec_to_raw,
        seed=lambda: parse_catalog(base_raw()), parse_error=CatalogError, invalidate=invalidate,
    )  # fmt: skip
    return svc, gw


async def live_pro_subscribers(db: FreshDb, n: int) -> None:
    async with db.migrator_uow() as uow:
        pro = await uow.plans.current("pro", None)
        assert pro is not None
        for i in range(n):
            acct = await uow.accounts.get_or_create(OwnerType.USER, uuid4(), f"p{i}@example.com")
            await uow.subscriptions.insert(
                SubscriptionRecord(
                    uuid4(),
                    acct.id,
                    SubscriptionState(SubStatus.ACTIVE, pro.plan.id, Interval.MONTH),
                )
            )


async def test_a_draft_is_validated_by_the_plans_yaml_loader_and_the_refusals(
    fresh_db: FreshDb,
) -> None:
    svc, _ = await make(fresh_db)

    async def errors(raw: dict) -> list[str]:  # type: ignore[type-arg]
        with pytest.raises(CatalogInvalid) as e:
            await svc.create_draft(ADMIN, raw, "")
        return e.value.errors

    r = draft_raw()
    r["plans"] = [p for p in r["plans"] if p["code"] != "pro"]
    assert any("plan 'pro' cannot be removed or renamed" in x for x in await errors(r))
    r = draft_raw()
    r["plans"] = [p for p in r["plans"] if p["code"] != "free"]
    assert any("free plan cannot be removed" in x for x in await errors(r))
    r = draft_raw()
    next(p for p in r["plans"] if p["code"] == "pro")["code"] = "pro_plus"  # a rename
    assert any("'pro' cannot be removed or renamed" in x for x in await errors(r))
    assert any(
        "catalog_version must change" in x
        for x in await errors({**draft_raw(), "catalog_version": "2026-11-15"})
    )
    r = draft_raw()
    r["model_groups"]["fast"].append({"id": "mystery-model", "label": "Mystery", "provider": "x"})
    assert any("mystery-model" in x and "no effective price" in x for x in await errors(r))
    r = draft_raw()
    next(p for p in r["plans"] if p["code"] == "pro")["prices"]["month"] = -5
    assert any("prices.month" in x for x in await errors(r))
    r = draft_raw()
    next(p for p in r["plans"] if p["code"] == "pro")["prices"]["month"] = 499.5  # not whole paise
    assert await errors(r)
    r = draft_raw()
    next(p for p in r["plans"] if p["code"] == "pro")["limits"]["messages_per_day"] = -7
    assert any("messages_per_day" in x for x in await errors(r))
    r = draft_raw()
    next(p for p in r["plans"] if p["code"] == "payg")["topup_packs"][0]["price"] = 199.5
    assert any("topup pack" in x for x in await errors(r))


async def test_the_diff_impact_and_warnings(fresh_db: FreshDb) -> None:
    svc, _ = await make(fresh_db)
    await live_pro_subscribers(fresh_db, 3)
    raw = draft_raw()
    pro = next(p for p in raw["plans"] if p["code"] == "pro")
    pro["limits"]["memory_facts"] = 100  # 500 -> 100: a limit goes down
    pro["prices"]["year"] = 1_500_000  # a yearly price at the UPI ceiling
    payg = next(p for p in raw["plans"] if p["code"] == "payg")
    payg["unit_prices"]["smart"] = 60
    payg["topup_packs"].append({"code": "ind_2999", "price": 299900})
    d = await svc.create_draft(ADMIN, raw, "raise Pro")
    (plan,) = [p for p in d["diff"]["plans"] if p["plan_code"] == "pro"]
    assert {"interval": "month", "from": 49900, "to": 59900} in plan["prices"]
    limits = {x["name"]: x for x in plan["limits"]}
    assert (
        limits["messages_per_day"]["from"],
        limits["messages_per_day"]["to"],
        limits["messages_per_day"]["direction"],
    ) == (60, 65, "up")
    assert limits["memory_facts"]["direction"] == "down"
    assert {"key": "smart", "from": 50, "to": 60} in d["diff"]["payg"]["unit_prices"]
    assert d["diff"]["payg"]["topup_packs"]["added"] == [{"code": "ind_2999", "price": 299900}]
    assert (
        d["impact"]["plans"][0]["active_subscribers"] == 3 and d["impact"]["total_subscribers"] == 3
    )
    joined = " | ".join(d["warnings"])
    assert "new plan version" in joined and "Apply limits" in joined and "Rs 15,000" in joined
    stored = await svc.get_draft(UUID(d["draft_id"]))
    assert stored["status"] == "open" and stored["based_on_version"] == "2026-11-15"
    assert [x["id"] for x in await svc.list_drafts()] == [UUID(d["draft_id"])] or len(
        await svc.list_drafts()
    ) == 1


async def test_publish_creates_a_new_plan_version_and_syncs_the_gateway_plan(
    fresh_db: FreshDb,
) -> None:
    svc, gw = await make(fresh_db)
    await live_pro_subscribers(fresh_db, 2)
    d = await svc.create_draft(ADMIN, draft_raw(), "Pro 65 a day at 599")
    before = gw.created
    out = await svc.publish(ADMIN, UUID(d["draft_id"]), "plan sheet v2")
    assert out["catalog_version"] == "2026-12-01"
    assert any(line.startswith("pro v2") for line in out["sync"])
    assert gw.created > before  # the new price got its own gateway plan
    async with fresh_db.migrator_uow() as uow:
        rows = (
            await uow.connection.execute(
                sa.text(
                    "SELECT version, price_month_paise, (limits->>'messages_per_day')::int AS m, retired_at IS NULL AS live "
                    "FROM billing.plan_versions WHERE plan_code = 'pro' ORDER BY version"
                )
            )
        ).all()
        assert [(r.version, r.price_month_paise, r.m, r.live) for r in rows] == [
            (1, 49900, 60, False),
            (2, 59900, 65, True),
        ]
        # a price change is not auto-applied: both subscribers stay on version 1
        on_v1 = (
            await uow.connection.execute(
                sa.text(
                    "SELECT count(*) FROM billing.subscriptions s JOIN billing.plan_versions p ON p.id = s.plan_version_id "
                    "WHERE p.plan_code = 'pro' AND p.version = 1"
                )
            )
        ).scalar_one()
        assert on_v1 == 2
        active = await uow.catalog.get_active()
        assert active is not None and active["catalog_version"] == "2026-12-01"
        audit = (
            await uow.connection.execute(
                sa.text("SELECT reason FROM billing.admin_audit WHERE action = 'catalog.publish'")
            )
        ).scalar_one()
        assert audit == "plan sheet v2"
    cat = await svc.get_catalog()
    assert cat["source"] == "published draft" and cat["catalog_version"] == "2026-12-01"
    hist = [
        (h["plan_code"], h["version"], h["active_subscribers"])
        for h in cat["plan_versions"]
        if h["plan_code"] == "pro"
    ]
    assert hist == [("pro", 1, 2), ("pro", 2, 0)]


async def test_limits_that_only_rise_reach_subscribers_at_once_and_a_fall_waits_for_apply_limits(
    fresh_db: FreshDb,
) -> None:
    svc, _ = await make(fresh_db)
    await live_pro_subscribers(fresh_db, 2)
    raw = draft_raw()
    pro = next(p for p in raw["plans"] if p["code"] == "pro")
    pro["prices"] = {"month": 49900, "year": 499000}  # same prices: a limits-only change
    d = await svc.create_draft(ADMIN, raw, "")
    out = await svc.publish(ADMIN, UUID(d["draft_id"]), "raise it")
    assert [(a["plan_code"], a["subscriptions_moved"]) for a in out["limits_raised_applied"]] == [
        ("pro", 2)
    ]
    assert out["apply_limits"] == []

    raw2 = copy.deepcopy(raw)
    raw2["catalog_version"] = "2026-12-15"
    next(p for p in raw2["plans"] if p["code"] == "pro")["limits"]["messages_per_day"] = 40
    d2 = await svc.create_draft(ADMIN, raw2, "")
    out2 = await svc.publish(ADMIN, UUID(d2["draft_id"]), "lower it")
    (needs,) = out2["apply_limits"]
    assert (
        needs["plan_code"] == "pro"
        and needs["limits_lowered"] == ["messages_per_day"]
        and isinstance(needs["audit_id"], int)
    )


async def test_a_stale_draft_is_a_409_and_a_draft_publishes_once(fresh_db: FreshDb) -> None:
    svc, _ = await make(fresh_db)
    first = await svc.create_draft(ADMIN, draft_raw(), "")
    second_raw = draft_raw()
    second_raw["catalog_version"] = "2026-12-02"
    second = await svc.create_draft(ADMIN, second_raw, "")  # both based on 2026-11-15
    await svc.publish(ADMIN, UUID(first["draft_id"]), "first")
    with pytest.raises(CatalogConflict, match="since this draft was made"):
        await svc.publish(ADMIN, UUID(second["draft_id"]), "second")
    with pytest.raises(CatalogConflict, match="already published"):
        await svc.publish(ADMIN, UUID(first["draft_id"]), "again")
    with pytest.raises(Exception, match="reason"):
        await svc.publish(ADMIN, UUID(second["draft_id"]), " ")


async def test_the_export_round_trips_and_after_publish_it_is_the_published_catalog(
    fresh_db: FreshDb,
) -> None:
    svc, _ = await make(fresh_db)
    seed_spec = parse_catalog(base_raw())
    assert parse_catalog(yaml.safe_load(raw_to_yaml(await svc.export_raw()))) == seed_spec
    d = await svc.create_draft(ADMIN, draft_raw(), "")
    await svc.publish(ADMIN, UUID(d["draft_id"]), "go ahead")
    exported = parse_catalog(yaml.safe_load(raw_to_yaml(await svc.export_raw())))
    assert exported == parse_catalog(draft_raw())
    assert (ROOT / "config" / "plans.yaml").exists()


async def test_over_http_support_reads_but_cannot_write_or_publish(fresh_db: FreshDb) -> None:
    svc, _ = await make(fresh_db)
    app = FastAPI()
    app.include_router(admin_routes.router)
    install_errors(app)
    app.state.catalog_admin = svc

    def hdr(role: str) -> dict[str, str]:
        return {"X-User-Id": str(uuid4()), "X-Staff-Role": role}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/admin/billing/v1/catalog", headers=hdr("support"))
        assert r.status_code == 200 and r.json()["source"] == "yaml"
        assert (
            await c.get("/admin/billing/v1/catalog/export.yaml", headers=hdr("support"))
        ).status_code == 200
        assert (
            await c.post(
                "/admin/billing/v1/catalog/drafts",
                json={"catalog": draft_raw()},
                headers=hdr("support"),
            )
        ).status_code == 403
        made = await c.post(
            "/admin/billing/v1/catalog/drafts",
            json={"catalog": draft_raw(), "note": "n"},
            headers=hdr("billing_admin"),
        )
        assert made.status_code == 201
        draft_id = made.json()["draft_id"]
        pub = f"/admin/billing/v1/catalog/drafts/{draft_id}/publish"
        assert (
            await c.post(pub, json={"reason": "go ahead"}, headers=hdr("support"))
        ).status_code == 403
        assert (
            await c.post(pub, json={"reason": ""}, headers=hdr("billing_admin"))
        ).status_code == 422
        ok = await c.post(pub, json={"reason": "go ahead"}, headers=hdr("billing_admin"))
        assert ok.status_code == 200 and ok.json()["catalog_version"] == "2026-12-01"
        stale = await c.post(pub, json={"reason": "again"}, headers=hdr("billing_admin"))
        assert stale.status_code == 409 and stale.json()["code"] == "CATALOG_CONFLICT"
        bad = await c.post(
            "/admin/billing/v1/catalog/drafts",
            json={"catalog": {**draft_raw(), "catalog_version": "2026-12-01"}},
            headers=hdr("billing_admin"),
        )
        assert bad.status_code == 422 and bad.json()["errors"]
        assert (await c.get("/admin/billing/v1/catalog/export.yaml")).status_code == 401
    assert Forbidden  # imported for the 403 mapping above


async def test_the_application_role_can_publish_but_cannot_rewrite_a_plan_version(
    fresh_db: FreshDb,
) -> None:
    """B4g: publish runs as billing_app. It may add versions and retire old ones, never change a price or a limit."""
    svc, _ = await make(fresh_db)
    d = await svc.create_draft(ADMIN, draft_raw(), "")
    await svc.publish(
        ADMIN, UUID(d["draft_id"]), "as the application role"
    )  # the whole path works with the app role
    for statement in (
        "UPDATE billing.plan_versions SET price_month_paise = 1",
        "UPDATE billing.plan_versions SET limits = '{}'::jsonb",
        "UPDATE billing.plan_versions SET checksum = 'x'",
        "DELETE FROM billing.plan_versions",
        "UPDATE billing.catalog_drafts SET catalog = '{}'::jsonb",
    ):
        with pytest.raises(sa.exc.DBAPIError, match="permission denied"):
            async with fresh_db.app_uow() as uow:
                await uow.connection.execute(sa.text(statement))
    async with fresh_db.app_uow() as uow:  # what publish needs is still allowed
        await uow.connection.execute(
            sa.text("UPDATE billing.plan_versions SET retired_at = retired_at")
        )
