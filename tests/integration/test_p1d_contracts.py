"""P1d: entitlements.changed, usage.metered, attempt-aware usage ids and the unregistered-GST
wallet debit."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio

from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
from billing.adapters.kafka.producer import KafkaProducer
from billing.adapters.kafka.usage_consumer import USAGE_TOPICS, make_usage_consumer, to_usage_event
from billing.app import topics
from billing.app.admin import Staff
from billing.app.entitlement_events import announce, announce_owner
from billing.app.entitlements import ModelCatalog
from billing.app.org_membership import OrgMembershipService
from billing.app.reserve import ReserveService
from billing.app.usage import MeteredEvent, UsageService
from billing.app.wallet import WalletService
from billing.domain.catalog import OrgVariant, OwnerType, ReserveKind
from billing.domain.money import Paise
from billing.domain.rating import UnknownModelPrice
from tests.integration.billing_env import Env
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import make_stack, make_user
from tests.integration.kafka_support import make_topic, read_all
from tests.integration.test_wallet import ULTRA, payg_prices, spend, top_up, wallet_ledger
from tests.support.clock import TestClock

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
STAFF = Staff(uuid4(), "billing_admin")


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# --- entitlements.changed -----------------------------------------------------------------


async def ent_events(env: Env, user: UUID | None = None) -> list[dict[str, object]]:
    rows = await env.sql(
        "SELECT payload FROM billing.outbox WHERE event_type = 'entitlements.changed' ORDER BY id"
    )
    events = [json.loads(bytes(r[0])) for r in rows]
    return [e for e in events if user is None or e["user_id"] == str(user)]


async def reasons(env: Env, user: UUID | None = None) -> list[object]:
    return [e["reason"] for e in await ent_events(env, user or env.user)]


async def test_a_subscription_change_announces_once_with_the_contract_fields(env: Env) -> None:
    assert await ent_events(env) == []
    await env.activate()
    (event,) = await ent_events(env)
    assert event["event_type"] == "entitlements.changed" and event["schema_version"] == "1.0"
    assert event["producer"] == "billing-service" and event["user_id"] == str(env.user)
    assert event["reason"] == "subscription" and event["occurred_at"].endswith("Z")  # type: ignore[attr-defined]
    assert str(event["version_hash"]).startswith("sha256:") and event["event_id"]
    await env.service.cancel(env.user)
    assert await reasons(env) == ["subscription", "subscription"]


async def test_overrides_and_blocks_announce_once_each(env: Env) -> None:
    await env.activate()
    base = len(await ent_events(env))
    admin = env.admin
    expires = env.clock.now() + timedelta(days=10)
    record = await admin.create_override(STAFF, OwnerType.USER, env.user, "max", "pilot", expires)
    await admin.end_override(STAFF, record.id, "pilot over")
    await admin.end_override(STAFF, record.id, "twice")  # already ended: nothing changes
    account = UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
    await admin.block(STAFF, account, "chargeback", blocked=True)
    await admin.block(STAFF, account, "resolved", blocked=False)
    assert (await reasons(env))[base:] == ["override", "override", "block", "block"]


async def test_the_event_is_rolled_back_with_its_transaction(env: Env) -> None:
    with pytest.raises(RuntimeError):
        async with env.uow() as uow:
            await announce(uow, env.user, "block")
            raise RuntimeError("the change failed after the announcement")
    assert await ent_events(env) == []
    async with env.uow() as uow:
        await announce(uow, env.user, "block")
    assert await reasons(env) == ["block"]


async def test_a_first_wallet_announces_once(env: Env) -> None:
    wallet = WalletService(env.bctx, env.counters)
    await wallet.set_cap(env.user, None)
    await wallet.set_cap(env.user, Paise(100000))
    assert await reasons(env) == ["wallet"]


async def test_org_membership_announces_once_even_when_replayed(env: Env) -> None:
    service = OrgMembershipService(
        env.bctx.uow_factory, env.orgs, env.entitlements.invalidate, env.clock
    )
    org = uuid4()
    await service.apply(joined=True, user_id=env.user, org_id=org, dedupe_key="evt-1")
    await service.apply(joined=True, user_id=env.user, org_id=org, dedupe_key="evt-1")
    assert await reasons(env) == ["org"]
    await service.apply(joined=False, user_id=env.user, org_id=org, dedupe_key="evt-2")
    assert await reasons(env) == ["org", "org"]


async def test_an_org_change_announces_one_event_per_member(env: Env) -> None:
    org, members = uuid4(), [uuid4(), uuid4(), uuid4()]
    for m in members:
        env.orgs.join(m, org, OrgVariant.COMPANY)
    with pytest.raises(RuntimeError):  # rolled back: no job, so no events
        async with env.uow() as uow:
            await announce_owner(uow, OwnerType.ORG, org, "subscription", env.clock.now())
            raise RuntimeError("rolled back")
    await env.run_jobs()
    assert await ent_events(env) == []
    async with env.uow() as uow:
        await announce_owner(uow, OwnerType.ORG, org, "subscription", env.clock.now())
    await env.run_jobs()
    await env.run_jobs()
    events = await ent_events(env)
    assert sorted(str(e["user_id"]) for e in events) == sorted(str(m) for m in members)
    assert {e["reason"] for e in events} == {"subscription"}


async def test_apply_limits_announces_every_moved_subscriber(env: Env) -> None:
    await env.activate()
    old = await env.scalar("SELECT plan_version_id FROM billing.subscriptions")
    new = uuid4()
    async with env.migrator_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO billing.plan_versions (id, plan_code, variant, version, rank, "
                "price_month_paise, price_year_paise, limits, allowed_models, effective_from, "
                "checksum, default_group) SELECT :n, plan_code, variant, version + 1, rank, "
                "price_month_paise, price_year_paise, limits || '{\"messages_per_day\": 99}', "
                "allowed_models, now(), 'v2', default_group FROM billing.plan_versions WHERE id = :o"  # noqa: E501
            ),
            {"n": new, "o": old},
        )
        audit_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO billing.admin_audit (actor_id, action, target_type, target_id, "
                    "reason, before, after) VALUES (:a, 'catalog.limits_only_change', "
                    "'plan_version', :n, 'sync', CAST(:b AS jsonb), CAST(:f AS jsonb)) RETURNING id"
                ),
                {"a": uuid4(), "n": str(new), "b": json.dumps({"plan_version_id": str(old)}),
                 "f": json.dumps({"plan_version_id": str(new)})},
            )
        ).scalar_one()  # fmt: skip
    before = len(await ent_events(env))
    assert await env.admin.apply_limits(STAFF, audit_id, "roll out") == 1
    assert await env.admin.apply_limits(STAFF, audit_id, "again") == 0
    assert (await reasons(env))[before:] == ["catalog"]


# --- usage.metered ------------------------------------------------------------------------


def seeded_cost_micro_inr(model: str, input_tokens: int, output_tokens: int) -> int:
    """Rate a call straight from the seed files, independently of the code under test."""
    price = next(
        p for p in load_model_prices(ROOT / "config/seed/model_prices.yaml") if p.model == model
    )
    fx = load_fx_rates(ROOT / "config/seed/fx_rates.yaml")[-1].usd_inr
    usd = (input_tokens * price.input_usd_per_m + output_tokens * price.output_usd_per_m) / Decimal(
        1_000_000
    )
    return int((usd * fx * 1_000_000).to_integral_value())


def metered_json(
    uid: UUID, *, model: str = "openai/gpt-oss-20b", fixed: int = 0, event_id: str | None = None
) -> bytes:
    return json.dumps(
        {
            "event_type": "usage.metered",
            "schema_version": "1.0",
            "event_id": event_id or str(uuid4()),
            "occurred_at": iso(NOW),
            "producer": "memory-service",
            "user_id": str(uid),
            "conversation_id": None,
            "request_message_id": None,
            "feature": "memory.summary",
            "calls": [
                {
                    "provider": "groq",
                    "model": model,
                    "purpose": "memory.summary",
                    "input_tokens": 2000,
                    "cached_input_tokens": 0,
                    "output_tokens": 300,
                    "usage_estimated": False,
                }
            ],
            "fixed_cost_micro_inr": fixed,
            "something_new": 1,
        }
    ).encode()


async def usage_rows(hot: FreshDb, uid: UUID, kind: str) -> list[sa.Row[tuple[object, ...]]]:
    async with hot.app_uow() as uow:
        res = await uow.connection.execute(
            sa.text(
                "SELECT source_id, billable_units, cost_micro_inr, price_paise, kind, "
                "aux_input_tokens, aux_output_tokens FROM billing.usage_events "
                "WHERE user_id = :u AND kind = :k ORDER BY source_id"
            ),
            {"u": uid, "k": kind},
        )
        return list(res.all())


async def test_the_memory_service_example_rates_to_exact_micro_inr(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    uid = await make_user(hot, "pro")
    stack = make_stack(hot, vk, TestClock(NOW))
    service = UsageService(lambda: hot.app_uow(), stack.entitlements, stack.counters, stack.clock)
    expected = seeded_cost_micro_inr("openai/gpt-oss-20b", 2000, 300)
    assert expected == 22_800  # 2,000 in and 300 out at US$0.075 / 0.30 per million, 95 INR/USD

    raw = metered_json(uid, fixed=1_000, event_id="evt-m-1")
    event = to_usage_event(topics.USAGE_METERED, raw)
    assert isinstance(event, MeteredEvent)
    assert await service.apply_metered(event) == "applied"
    assert await service.apply_metered(event) == "duplicate"  # redelivery
    ((source, units, cost, price, kind, aux_in, aux_out),) = await usage_rows(hot, uid, "aux")
    assert (source, units, price, kind) == ("evt-m-1", 0, 0, "aux")
    assert cost == expected + 1_000 == 23_800
    assert (aux_in, aux_out) == (2000, 300)
    # it never touches counters
    counts = await stack.counters.day_counts(uid, NOW.date())
    assert counts.get("total", 0) == 0
    assert await usage_rows(hot, uid, "message") == []


async def test_an_unknown_model_is_refused(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    uid = await make_user(hot, "pro")
    stack = make_stack(hot, vk, TestClock(NOW))
    service = UsageService(lambda: hot.app_uow(), stack.entitlements, stack.counters, stack.clock)
    event = to_usage_event(topics.USAGE_METERED, metered_json(uid, model="mystery-model-9"))
    assert isinstance(event, MeteredEvent)
    with pytest.raises(UnknownModelPrice):
        await service.apply_metered(event)
    assert await usage_rows(hot, uid, "aux") == []  # nothing half-written


@pytest_asyncio.fixture
async def producer(kafka: str) -> AsyncIterator[KafkaProducer]:
    for t in (*USAGE_TOPICS, topics.BILLING_USAGE_DLQ):
        await make_topic(kafka, t, partitions=3)
    p = KafkaProducer(kafka, "test")
    await p.start()
    yield p
    await p.stop()


async def test_the_consumer_applies_metered_events_and_dead_letters_unknown_models(
    hot: FreshDb, vk: valkey_asyncio.Valkey, kafka: str, producer: KafkaProducer
) -> None:
    uid = await make_user(hot, "pro")
    key = str(uid).encode()
    stack = make_stack(hot, vk, TestClock(NOW))
    service = UsageService(lambda: hot.app_uow(), stack.entitlements, stack.counters, stack.clock)
    consumer = make_usage_consumer(
        service,
        bootstrap_servers=kafka,
        client_id="t",
        producer=producer,
        group_id=f"g-{uuid4()}",
        backoff=(0.01,) * 5,
    )
    assert consumer is not None and topics.USAGE_METERED in USAGE_TOPICS
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        good = metered_json(uid, event_id="evt-k-1")
        await producer.send_and_wait(topics.USAGE_METERED, key, good)
        await producer.send_and_wait(topics.USAGE_METERED, key, good)
        await producer.send_and_wait(topics.USAGE_METERED, key, metered_json(uid, model="nope-1"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            letters = [
                r
                for r in await read_all(kafka, topics.BILLING_USAGE_DLQ, quiet=1.0)
                if r.key == key
            ]
            if len(await usage_rows(hot, uid, "aux")) == 1 and len(letters) == 1:
                break
            await asyncio.sleep(0.5)
        assert len(await usage_rows(hot, uid, "aux")) == 1
        assert len(letters) == 1 and letters[0].headers["source_topic"] == b"usage.metered"
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)


# --- attempt-aware source ids -------------------------------------------------------------


def manim_json(uid: UUID, job: str, status: str, attempt: int | None) -> bytes:
    body: dict[str, object] = {
        "event_type": "artifact.manim.rendered",
        "event_id": str(uuid4()),
        "occurred_at": iso(NOW),
        "user_id": str(uid),
        "job_id": job,
        "hold_id": None,
        "status": status,
        "calls": [
            {
                "provider": "groq",
                "model": "openai/gpt-oss-20b",
                "purpose": "manim_plan",
                "input_tokens": 500,
                "output_tokens": 100,
            }  # fmt: skip
        ],
    }
    if attempt is not None:
        body["attempt"] = attempt
    return json.dumps(body).encode()


async def test_a_manim_retry_that_succeeds_is_billed_after_a_failed_first_attempt(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    uid = await make_user(hot, "pro")
    stack = make_stack(hot, vk, TestClock(NOW))
    service = UsageService(lambda: hot.app_uow(), stack.entitlements, stack.counters, stack.clock)
    for status, attempt in (("error", 1), ("success", 2)):
        event = to_usage_event(
            topics.ARTIFACT_MANIM_RENDERED, manim_json(uid, "job-7", status, attempt)
        )
        assert not isinstance(event, MeteredEvent)
        assert event.source_id == f"job-7:{attempt}" and event.kind is ReserveKind.MANIM_VIDEO
        await service.apply(event)
    rows = await usage_rows(hot, uid, "manim_video")
    assert [(r[0], r[1]) for r in rows] == [("job-7:1", 0), ("job-7:2", 1)]  # the retry is billed


async def test_old_events_without_an_attempt_use_attempt_one(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    uid = await make_user(hot, "pro")
    old = to_usage_event(
        topics.ARTIFACT_MANIM_RENDERED, manim_json(uid, "job-old", "success", None)
    )
    assert not isinstance(old, MeteredEvent) and old.source_id == "job-old:1"
    youtube = {
        "event_id": "e1", "occurred_at": iso(NOW), "user_id": str(uid), "status": "success",
        "source_id": "doc-1:2", "document_id": "doc-1", "attempt": 2,
    }  # fmt: skip
    ev = to_usage_event(topics.FILE_YOUTUBE_INGESTED, json.dumps(youtube).encode())
    assert not isinstance(ev, MeteredEvent) and ev.source_id == "doc-1:2"
    legacy = {k: v for k, v in youtube.items() if k not in ("document_id", "attempt")}
    legacy["source_id"] = "doc-9"
    ev = to_usage_event(topics.FILE_YOUTUBE_INGESTED, json.dumps(legacy).encode())
    assert not isinstance(ev, MeteredEvent) and ev.source_id == "doc-9:1"


# --- GST_REGISTERED=false: the wallet is debited at the full unit price --------------------


async def test_unregistered_wallet_debits_the_full_price_so_no_revenue_is_lost(env: Env) -> None:
    env.with_config(replace(env.bctx.config, gst_registered=False))
    prices = payg_prices()
    reserve = ReserveService(
        env.entitlements,
        ModelCatalog(env.bctx.uow_factory, env.clock),
        env.counters,
        env.clock,
        env.bctx.ids,
        payg=prices,
        gst_registered=False,
    )
    usage = UsageService(
        env.bctx.uow_factory,
        env.entitlements,
        env.counters,
        env.clock,
        payg=prices,
        gst_registered=False,
    )
    wallet = WalletService(env.bctx, env.counters)
    await top_up(env, wallet)  # Rs 199, credited gross because there is no GST to take out
    assert (await wallet.view(env.user))["balance_paise"] == 19900  # type: ignore[index]
    assert await wallet_ledger(env) == 19900

    sent = 0
    while (outcome := await spend(env, reserve, usage, sent)) == "credits":
        sent += 1
        assert sent < 100
    assert outcome == "no_credit"
    assert sent == 19900 // ULTRA == 16  # taxable_of(1200) = 1017 would have allowed 19
    taken = 19900 - await wallet_ledger(env)
    assert taken == sent * ULTRA  # every message took its whole price: nothing leaks
    price_recorded = await env.scalar(
        "SELECT sum(price_paise) FROM billing.usage_events WHERE kind = 'message'"
    )
    assert price_recorded == taken
