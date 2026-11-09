from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
import yaml

from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
from billing.adapters.catalog.yaml_loader import parse_catalog
from billing.adapters.ids import Uuid7Generator
from billing.app.catalog import CatalogSyncError, SyncAction, sync_catalog
from billing.app.records import SubscriptionRecord
from billing.domain.catalog import Interval, ModelGroup, OwnerType, PaygPrices
from billing.domain.rating import LlmCall, NoFxRate, rate
from billing.domain.subscription import SubscriptionState, SubStatus
from billing.main_api import app
from tests.integration.conftest import FreshDb
from tests.support import plans
from tests.support.clock import TestClock
from tests.support.fake_gateway import FakeGateway

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "config" / "plans.yaml").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def spec_of(text: str = TEXT, drop: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    raw = yaml.safe_load(text)
    raw["plans"] = [p for p in raw["plans"] if p["code"] not in drop]
    return parse_catalog(raw)


async def seed(db: FreshDb) -> None:
    async with db.migrator_uow() as uow:
        for p in load_model_prices(ROOT / "config/seed/model_prices.yaml"):
            await uow.pricing.add_model_price(
                model=p.model,
                provider=p.provider,
                model_group=p.group,
                input_usd_per_m=p.input_usd_per_m,
                cached_input_usd_per_m=p.cached_input_usd_per_m,
                output_usd_per_m=p.output_usd_per_m,
                effective_from=p.effective_from,
            )
        for r in load_fx_rates(ROOT / "config/seed/fx_rates.yaml"):
            await uow.pricing.add_fx_rate(r.effective_from, r.usd_inr, r.source)


async def run(
    db: FreshDb,
    gw: FakeGateway,
    text: str = TEXT,
    *,
    dry_run: bool = False,
    drop: tuple[str, ...] = (),
):  # type: ignore[no-untyped-def]
    return await sync_catalog(
        spec_of(text, drop), db.migrator_uow, gw, TestClock(NOW), Uuid7Generator(), dry_run=dry_run
    )


async def versions(db: FreshDb, code: str) -> list[sa.Row[tuple[object, ...]]]:
    async with db.migrator_uow() as uow:
        rows = await uow.connection.execute(
            sa.text("SELECT * FROM billing.plan_versions WHERE plan_code = :c ORDER BY version"),
            {"c": code},
        )
        return list(rows)


def by_label(report) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {o.label: o for o in report.outcomes}


async def test_first_sync_creates_everything_second_is_a_noop(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    first = await run(fresh_db, gw)
    out = by_label(first)
    assert {o.action for o in out.values()} == {SyncAction.CREATED}
    assert {"free", "pro", "max", "payg", "org_pro/college", "org_max/company"} <= set(out)
    assert len(gw.plans) == 4  # pro and max, month and year
    assert first.gateway_plans_created == 4
    names = sorted(p.name for p in gw.plans.values())
    assert names == [
        "GranthanAI Max · monthly",
        "GranthanAI Max · yearly",
        "GranthanAI Pro · monthly",
        "GranthanAI Pro · yearly",
    ]
    pro_m = next(p for p in gw.plans.values() if p.name == "GranthanAI Pro · monthly")
    assert (pro_m.period, pro_m.interval, pro_m.amount, pro_m.currency) == (
        "monthly",
        1,
        49900,
        "INR",
    )
    assert pro_m.notes["plan_code"] == "pro" and pro_m.notes["version"] == "1"
    pro = (await versions(fresh_db, "pro"))[0]
    assert pro.razorpay_plan_month and pro.razorpay_plan_year
    assert (await versions(fresh_db, "free"))[0].razorpay_plan_month is None

    calls_before = len(gw.calls)
    second = await run(fresh_db, gw)
    assert {o.action for o in second.outcomes} == {SyncAction.UNCHANGED}
    assert len(gw.calls) == calls_before  # zero gateway calls
    assert not second.changed


async def test_price_change_creates_version_two(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    v1_before = (await versions(fresh_db, "pro"))[0]

    report = await run(
        fresh_db, gw, TEXT.replace("prices: {month: 49900,", "prices: {month: 59900,")
    )
    pro = by_label(report)["pro"]
    assert pro.action is SyncAction.PRICE_CHANGE and pro.version == 2
    assert by_label(report)["max"].action is SyncAction.UNCHANGED

    v1, v2 = await versions(fresh_db, "pro")
    assert v2.price_month_paise == 59900 and v2.retired_at is None
    assert v2.razorpay_plan_month and v2.razorpay_plan_month != v1.razorpay_plan_month
    # version 1 is untouched apart from retired_at
    changed = {k for k in v1._mapping if v1._mapping[k] != v1_before._mapping[k]}
    assert changed == {"retired_at"}
    assert v1.retired_at == NOW
    assert len(gw.plans) == 6


async def test_limits_only_change_is_audited(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    text = TEXT.replace("      memory_facts: 2000\n", "      memory_facts: 2500\n")
    assert text != TEXT
    report = await run(fresh_db, gw, text)
    mx = by_label(report)["max"]
    assert mx.action is SyncAction.LIMITS_ONLY and mx.version == 2
    async with fresh_db.migrator_uow() as uow:
        rows = list(
            await uow.connection.execute(
                sa.text("SELECT action, target_type, after FROM billing.admin_audit")
            )
        )
    # the organisation Max variants inherit Max's limits, so they change too
    assert [r.action for r in rows] == ["catalog.limits_only_change"] * 3
    assert {r.after["limits"]["memory_facts"] for r in rows} == {2500}
    assert {o.label for o in report.outcomes if o.action is SyncAction.LIMITS_ONLY} == {
        "max",
        "org_max/college",
        "org_max/company",
    }
    # the price did not change, but the new version still needs its own gateway plans
    assert len(gw.plans) == 6


async def test_crash_after_insert_self_heals_without_duplicates(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    crashing = FakeGateway(crash_after_creates=1)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await run(fresh_db, crashing)
    # versions were committed before the gateway was called; no ids were saved yet
    assert {r.plan_code for r in await versions(fresh_db, "pro")} == {"pro"}
    assert (await versions(fresh_db, "pro"))[0].razorpay_plan_month is None
    assert len(crashing.plans) == 1

    report = await run(fresh_db, crashing)  # same fake: its plans survive the "crash"
    assert len(crashing.plans) == 4  # the first plan was found by checksum, not recreated
    assert {o.action for o in report.outcomes} == {SyncAction.UNCHANGED}
    pro = (await versions(fresh_db, "pro"))[0]
    assert pro.razorpay_plan_month and pro.razorpay_plan_year


async def _live_max_subscription(db: FreshDb) -> None:
    async with db.migrator_uow() as uow:
        acct = await uow.accounts.get_or_create(OwnerType.USER, uuid4(), "m@example.com")
        mx = await uow.plans.current("max", None)
        assert mx is not None
        await uow.subscriptions.insert(
            SubscriptionRecord(
                uuid4(), acct.id, SubscriptionState(SubStatus.ACTIVE, mx.plan.id, Interval.MONTH)
            )
        )


async def test_removed_plan_with_live_subscription_fails_and_changes_nothing(
    fresh_db: FreshDb,
) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    await _live_max_subscription(fresh_db)
    before = [r._mapping for r in await versions(fresh_db, "max")]
    changed = TEXT.replace("prices: {month: 49900,", "prices: {month: 59900,")  # also pending
    with pytest.raises(CatalogSyncError, match="plan 'max'.*1 live subscription"):
        await run(fresh_db, gw, changed, drop=("max",))
    assert [r._mapping for r in await versions(fresh_db, "max")] == before
    assert len(await versions(fresh_db, "pro")) == 1  # nothing else was applied either


async def test_removed_plan_without_subscriptions_is_retired(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    report = await run(fresh_db, gw, drop=("max",))
    assert by_label(report)["max"].action is SyncAction.RETIRED
    (mx,) = await versions(fresh_db, "max")
    assert mx.retired_at == NOW


async def test_dry_run_writes_and_calls_nothing(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    report = await run(fresh_db, gw, dry_run=True)
    assert {o.action for o in report.outcomes} == {SyncAction.CREATED}
    assert report.gateway_plans_created == 4  # reported, not performed
    assert gw.calls == [] and gw.plans == {}
    assert await versions(fresh_db, "pro") == []
    assert all(line.startswith("[dry run]") for line in report.lines())

    await run(fresh_db, gw)
    changed = TEXT.replace("prices: {month: 49900,", "prices: {month: 59900,")
    n = len(gw.calls)
    dry = await run(fresh_db, gw, changed, dry_run=True)
    assert by_label(dry)["pro"].action is SyncAction.PRICE_CHANGE
    assert len(gw.calls) == n and len(await versions(fresh_db, "pro")) == 1


async def test_prices_and_fx_are_effective_dated(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    async with fresh_db.migrator_uow() as uow:
        await uow.pricing.add_model_price(  # synthetic: the doubled price from 1 Jan 2027
            model="gemini-2.5-flash",
            provider="google",
            model_group=ModelGroup.SMART,
            input_usd_per_m=Decimal("0.60"),
            cached_input_usd_per_m=None,
            output_usd_per_m=Decimal("5.00"),
            effective_from=datetime.fromisoformat("2027-01-01T00:00:00+05:30"),
        )
    async with fresh_db.app_uow() as uow:
        old = await uow.pricing.price_book_at(datetime(2026, 12, 31, 18, 29, 59, tzinfo=UTC))
        new = await uow.pricing.price_book_at(datetime(2026, 12, 31, 18, 30, 0, tzinfo=UTC))
        assert old.for_model("gemini-2.5-flash").input_usd_per_m == Decimal("0.30")
        assert new.for_model("gemini-2.5-flash").input_usd_per_m == Decimal("0.60")
        assert old.for_model("claude-haiku-5-5") == new.for_model("claude-haiku-5-5")
        assert old.models() == plans.SPEC.all_models()

        assert await uow.pricing.fx_at(NOW) == Decimal("95.0000")
        with pytest.raises(NoFxRate):
            await uow.pricing.fx_at(datetime(2026, 10, 24, 18, 29, tzinfo=UTC))  # 24 Oct, IST
        assert await uow.pricing.fx_at(datetime(2026, 10, 24, 18, 30, tzinfo=UTC)) == 95

        # the Phase 1 workbook message, priced from the database
        calls = [
            LlmCall("anthropic", "claude-haiku-5-5", "answer", 6000, 0, 700),
            LlmCall("groq", "openai/gpt-oss-20b", "request_analysis", 2000, 0, 250),
            LlmCall("groq", "openai/gpt-oss-20b", "memory", 600, 0, 100),
        ]
        r = rate(
            calls,
            await uow.pricing.price_book_at(NOW),
            await uow.pricing.fx_at(NOW),
            ModelGroup.SMART,
            "plan",
            PaygPrices({}),
        )
        assert r.cost == 118_750


async def test_sync_fails_when_a_model_has_no_price(fresh_db: FreshDb) -> None:
    async with fresh_db.migrator_uow() as uow:  # only one model priced
        await uow.pricing.add_model_price(
            model="claude-haiku-5-5",
            provider="anthropic",
            model_group=ModelGroup.SMART,
            input_usd_per_m=Decimal("0.10"),
            cached_input_usd_per_m=None,
            output_usd_per_m=Decimal("0.50"),
            effective_from=datetime(2026, 10, 25, tzinfo=UTC),
        )
    gw = FakeGateway()
    with pytest.raises(CatalogSyncError, match="gemini-2.5-flash") as exc:
        await run(fresh_db, gw)
    assert "claude-haiku-5-5" not in str(exc.value)
    assert await versions(fresh_db, "pro") == []


async def test_get_plans_endpoint(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    await run(fresh_db, FakeGateway())
    app.state.engine = fresh_db.app_engine
    app.state.catalog_version = "2026-11-01"
    app.state.payg_enabled = False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/billing/v1/plans")  # no auth header
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=300"
        body = r.json()
        assert body["catalog_version"] == "2026-11-01"
        assert (body["currency"], body["gst_inclusive"]) == ("INR", True)
        codes = [p["code"] for p in body["plans"]]
        assert codes == ["free", "pro", "max"]  # payg and org plans hidden
        pro, mx = body["plans"][1], body["plans"][2]
        assert pro["prices"] == {"month": 49900, "year": 499000}
        assert mx["prices"] == {"month": 149900, "year": 1499000}
        assert pro["groups"] == ["fast", "smart"] and pro["version"] == 1 and pro["rank"] == 1
        assert mx["limits"]["best_per_day"] == 12 and len(mx["limits"]) == 12
        assert body["plans"][0]["prices"] == {} and body["plans"][0]["extra_models"] == [
            "openai/gpt-oss-120b"
        ]

        etag = r.headers["etag"]
        again = await client.get("/billing/v1/plans", headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        stale = await client.get("/billing/v1/plans", headers={"If-None-Match": '"nope"'})
        assert stale.status_code == 200

        app.state.payg_enabled = True
        with_payg = await client.get("/billing/v1/plans")
        assert [p["code"] for p in with_payg.json()["plans"]] == ["free", "payg", "pro", "max"]
        assert with_payg.headers["etag"] != etag
        assert "org_pro" not in with_payg.text
    app.state.payg_enabled = False


async def models_table(db: FreshDb) -> list[tuple[object, ...]]:
    async with db.migrator_uow() as uow:
        rows = await uow.connection.execute(
            sa.text(
                "SELECT id, label, provider, model_group::text, sort_order "
                "FROM billing.models ORDER BY sort_order"
            )
        )
        return [tuple(r) for r in rows]


async def test_sync_stores_default_groups_and_the_models(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    report = await run(fresh_db, FakeGateway())
    assert report.models_changed == 11
    async with fresh_db.migrator_uow() as uow:
        rows = await uow.connection.execute(
            sa.text("SELECT plan_code, default_group::text FROM billing.plan_versions")
        )
        defaults = {(r[0], r[1]) for r in rows}
    assert defaults == {
        ("free", "fast"),
        ("pro", "smart"),
        ("max", "smart"),
        ("payg", "smart"),
        ("org_pro", "smart"),
        ("org_max", "smart"),
    }

    table = await models_table(fresh_db)
    assert len(table) == 11
    assert table[0] == ("openai/gpt-oss-20b", "GPT-OSS 20B", "groq", "fast", 0)
    assert table[7] == ("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", "best", 7)
    assert [r[4] for r in table] == list(range(11))
    async with fresh_db.app_uow() as uow:  # the app role reads the plan's default group
        pro = await uow.plans.current("pro", None)
        assert pro is not None and pro.plan.default_group is ModelGroup.SMART


async def test_a_label_change_updates_models_without_a_new_plan_version(
    fresh_db: FreshDb,
) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    versions_before = {c: len(await versions(fresh_db, c)) for c in ("free", "pro", "max")}
    calls_before = len(gw.calls)

    relabelled = TEXT.replace("label: GPT-OSS 20B,", "label: GPT-OSS Twenty-B,")
    assert relabelled != TEXT
    report = await run(fresh_db, gw, relabelled)
    assert report.models_changed == 1
    assert {o.action for o in report.outcomes} == {SyncAction.UNCHANGED}
    assert {c: len(await versions(fresh_db, c)) for c in ("free", "pro", "max")} == versions_before
    assert len(gw.calls) == calls_before  # no gateway plan was touched either
    assert (await models_table(fresh_db))[0][1] == "GPT-OSS Twenty-B"

    again = await run(fresh_db, gw, relabelled)
    assert again.models_changed == 0 and not again.changed


async def test_sync_removes_a_model_that_left_the_file(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    await run(fresh_db, FakeGateway())
    lines = [ln for ln in TEXT.splitlines() if "id: gpt-6-astra" not in ln]
    # gpt-6-astra stays priced; only the picker list shrinks
    report = await run(fresh_db, FakeGateway(), "\n".join(lines))
    assert report.models_changed == 1
    assert "gpt-6-astra" not in {r[0] for r in await models_table(fresh_db)}


async def test_default_group_change_is_a_new_version(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    gw = FakeGateway()
    await run(fresh_db, gw)
    pro = "rank: 1\n    default_group: "
    report = await run(fresh_db, gw, TEXT.replace(pro + "smart", pro + "fast", 1))
    out = by_label(report)["pro"]
    assert out.action is SyncAction.LIMITS_ONLY and out.version == 2
    assert [r.default_group for r in await versions(fresh_db, "pro")] == ["smart", "fast"]
