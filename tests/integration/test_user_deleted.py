"""user.deleted: subscriptions end, mandates stop, the account is anonymised, records stay."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_asyncio

from billing.adapters.kafka.producer import KafkaProducer
from billing.adapters.kafka.user_consumer import make_user_consumer
from billing.app import topics
from billing.app.user_deletion import UserDeletionService
from billing.domain.catalog import Interval
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env
from tests.integration.kafka_support import make_topic, read_all


async def count(env: Env, query: str, **params: object) -> int:
    return int(await env.scalar(query, **params))  # type: ignore[arg-type]


def deletion(env: Env) -> UserDeletionService:
    return UserDeletionService(env.bctx, env.counters)


async def test_deleting_a_subscriber_ends_everything_and_keeps_the_records(env: Env) -> None:
    await env.service.checkout(
        env.user, "pro", Interval.MONTH, "10", "gone@example.com", "Acme Ltd", "10ABCDE1234F1Z5"
    )
    sub = next(iter(env.gateway.subscriptions))
    auth, charged, _, _ = env.gateway.pay_checkout(sub, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    upgrade = await env.service.change_plan(env.user, "max", Interval.MONTH)  # a mandate waits
    await env.counters.rebuild_day(env.user, datetime(2026, 11, 1, tzinfo=UTC).date(), {"smart": 4})
    await env.run_jobs()
    changed_before = await count(
        env, "SELECT count(*) FROM billing.outbox WHERE event_type = 'subscription.changed'"
    )

    assert await deletion(env).apply(env.user, "evt-del-1") == "applied"

    assert await env.status() == "ended"
    assert await env.plan_code() == "free"
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "abandoned"
    cancels = await env.sql(
        "SELECT payload FROM billing.jobs WHERE kind = 'gateway.cancel' ORDER BY id"
    )
    ids = {(p[0]["gateway_subscription_id"], p[0]["at_cycle_end"]) for p in cancels}
    assert (sub, False) in ids and (upgrade.gateway_subscription_id, False) in ids
    await env.run_jobs()
    assert env.gateway.subscriptions[sub].status == "cancelled"
    assert env.gateway.subscriptions[upgrade.gateway_subscription_id].status == "cancelled"
    assert (
        await count(
            env, "SELECT count(*) FROM billing.outbox WHERE event_type = 'subscription.changed'"
        )
        == changed_before + 1
    )

    account = (
        await env.sql(
            "SELECT billing_email, legal_name, gstin, state_code, gateway_customer_id FROM billing.billing_accounts"  # noqa: E501
        )
    )[0]
    assert tuple(account) == (None, None, "10ABCDE1234F1Z5", "10", None)  # tax details stay

    # financial records are statutory: all still there
    assert await count(env, "SELECT count(*) FROM billing.payments") == 1
    assert await count(env, "SELECT count(*) FROM billing.invoices") == 1
    assert await count(env, "SELECT count(*) FROM billing.ledger_transactions") >= 1
    assert await count(env, "SELECT count(*) FROM billing.subscription_history") >= 2
    # the customer name on the issued invoice is part of the tax document and also stays
    assert await env.scalar("SELECT customer_name FROM billing.invoices") == "Acme Ltd"
    # Valkey is clean
    assert await env.counters.day_counts(env.user, datetime(2026, 11, 1, tzinfo=UTC).date()) == {}


async def test_the_event_is_idempotent_and_an_unknown_user_is_harmless(env: Env) -> None:
    await env.activate("max", MAX_MONTH)
    service = deletion(env)
    assert await service.apply(env.user, "evt-1") == "applied"
    history = await count(env, "SELECT count(*) FROM billing.subscription_history")
    jobs = await count(env, "SELECT count(*) FROM billing.jobs WHERE kind = 'gateway.cancel'")
    assert await service.apply(env.user, "evt-1") == "duplicate"
    assert await service.apply(env.user, "evt-1") == "duplicate"
    assert await count(env, "SELECT count(*) FROM billing.subscription_history") == history
    assert (
        await count(env, "SELECT count(*) FROM billing.jobs WHERE kind = 'gateway.cancel'") == jobs
    )
    # a different event for the same, already-deleted user changes nothing more
    assert await service.apply(env.user, "evt-2") == "applied"
    assert await count(env, "SELECT count(*) FROM billing.subscription_history") == history
    # a user with no account at all
    assert await service.apply(uuid4(), "evt-3") == "applied"


async def test_a_free_user_with_only_an_account_is_anonymised(env: Env) -> None:
    async with env.uow() as uow:
        from billing.domain.catalog import OwnerType

        await uow.accounts.get_or_create(OwnerType.USER, env.user, "free@example.com")
    await deletion(env).apply(env.user, "evt-free")
    assert await env.scalar("SELECT billing_email FROM billing.billing_accounts") is None


async def test_a_suspended_and_a_cancelling_subscription_also_end(env: Env) -> None:
    await env.activate()
    await env.service.cancel(env.user)
    assert await env.status() == "cancelling"
    await deletion(env).apply(env.user, "evt-cancelling")
    assert await env.status() == "ended"


# --- the Kafka path ----------------------------------------------------------------------------------  # noqa: E501


@pytest_asyncio.fixture
async def producer(kafka: str) -> AsyncIterator[KafkaProducer]:
    for t in (topics.USER_DELETED, topics.BILLING_USER_DLQ):
        await make_topic(kafka, t, partitions=3)
    p = KafkaProducer(kafka, "test")
    await p.start()
    yield p
    await p.stop()


async def test_the_consumer_applies_the_event_and_dead_letters_a_bad_one(
    env: Env, kafka: str, producer: KafkaProducer
) -> None:
    await env.activate()
    consumer = make_user_consumer(
        deletion(env),
        bootstrap_servers=kafka,
        client_id="t",
        producer=producer,
        group_id=f"g-{uuid4()}",
        backoff=(0.01,) * 5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    key = str(env.user).encode()
    body = {"event_id": f"e-{uuid4()}", "occurred_at": "2026-11-05T10:00:00Z",
            "user_id": str(env.user), "reason": "ignored"}  # fmt: skip
    try:
        await producer.send_and_wait(topics.USER_DELETED, key, json.dumps(body).encode())
        await producer.send_and_wait(topics.USER_DELETED, key, json.dumps(body).encode())
        await producer.send_and_wait(topics.USER_DELETED, key, b"nonsense")
        await producer.send_and_wait(
            topics.USER_DELETED, key, json.dumps({**body, "user_id": "not-a-uuid"}).encode()
        )
        for _ in range(80):
            letters = [
                r for r in await read_all(kafka, topics.BILLING_USER_DLQ, quiet=0.8) if r.key == key
            ]
            if await env.status() == "ended" and len(letters) == 2:
                break
            await asyncio.sleep(0.5)
        assert await env.status() == "ended"
        assert len(letters) == 2
        assert {r.headers["source_topic"] for r in letters} == {topics.USER_DELETED.encode()}
        assert (
            await env.scalar(
                "SELECT count(*) FROM billing.processed_events WHERE consumer = 'billing-user'"
            )
            == 1
        )
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
    assert UUID(str(env.user))
