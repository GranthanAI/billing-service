"""The billing-usage consumer end to end on Kafka: JSON in, usage rows out, bad input to the DLQ,
and the replay command."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest_asyncio
import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio

from billing.adapters.kafka import dlq
from billing.adapters.kafka.producer import KafkaProducer
from billing.adapters.kafka.usage_consumer import USAGE_TOPICS, make_usage_consumer
from billing.app import topics
from billing.app.usage import UsageService
from billing.domain.catalog import ModelGroup
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import make_stack, make_user
from tests.integration.kafka_support import make_topic, read_all
from tests.support.clock import TestClock
from tests.support.wait import wait_until

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def producer(kafka: str) -> AsyncIterator[KafkaProducer]:
    for t in (*USAGE_TOPICS, topics.BILLING_USAGE_DLQ):
        await make_topic(kafka, t, partitions=3)
    p = KafkaProducer(kafka, "test")
    await p.start()
    yield p
    await p.stop()


def chat_json(uid: UUID, rid: str, *, model: str = "claude-haiku-5-5") -> bytes:
    return json.dumps(
        {
            "event_type": "chat.response.generated",
            "schema_version": "2.2",
            "conversation_id": "c1",
            "user_id": str(uid),
            "request_message_id": rid,
            "response_message_id": f"r-{rid}",
            "status": "success",
            "timestamp": NOW.isoformat().replace("+00:00", "Z"),
            "hold_id": None,
            "model_group": "smart",
            "model": model,
            "calls": [
                {
                    "provider": "anthropic",
                    "model": model,
                    "purpose": "answer",
                    "input_tokens": 6000,
                    "cached_input_tokens": 0,
                    "output_tokens": 700,
                    "usage_estimated": False,
                }
            ],
            "something_new": {"ignored": True},
        }
    ).encode()


async def usage_rows(hot: FreshDb, uid: UUID) -> int:
    async with hot.app_uow() as uow:
        res = await uow.connection.execute(
            sa.text("SELECT count(*) FROM billing.usage_events WHERE user_id = :u"), {"u": uid}
        )
        return int(res.scalar_one())


async def dead_letters(kafka: str, key: bytes) -> list[tuple[dict[str, bytes], bytes]]:
    return [
        (r.headers, r.value)
        for r in await read_all(kafka, topics.BILLING_USAGE_DLQ, quiet=1.0)
        if r.key == key
    ]


wait_for = wait_until  # the shared 50 ms poller


async def test_json_events_are_applied_and_bad_input_is_dead_lettered_then_replayed(
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
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        good = chat_json(uid, "ok-1")
        topic = topics.CHAT_RESPONSE_GENERATED
        await producer.send_and_wait(topic, key, good)
        await producer.send_and_wait(topic, key, good)  # a duplicate delivery
        await producer.send_and_wait(topic, key, b"{not json")
        await producer.send_and_wait(topic, key, chat_json(uid, "mystery", model="mystery-model-1"))

        async def settled() -> bool:
            return await usage_rows(hot, uid) == 1 and len(await dead_letters(kafka, key)) == 2

        await wait_for(settled, 60)
        assert await usage_rows(hot, uid) == 1  # the good event once, despite the duplicate
        letters = await dead_letters(kafka, key)
        assert {h["source_topic"] for h, _ in letters} == {topic.encode()}
        assert any(v == b"{not json" for _, v in letters)

        # the price arrives: add it and replay; the unknown-model message now succeeds
        async with hot.migrator_uow() as uow:
            await uow.pricing.add_model_price(
                model="mystery-model-1",
                provider="groq",
                model_group=ModelGroup.SMART,
                input_usd_per_m=Decimal("0.10"),
                cached_input_usd_per_m=None,
                output_usd_per_m=Decimal("0.50"),
                effective_from=datetime(2026, 10, 1, tzinfo=UTC),
            )
        result = await dlq.replay(kafka, topics.BILLING_USAGE_DLQ, group_id=f"r-{uuid4()}")
        assert result.replayed >= 2 and result.skipped == 0

        async def both_rows() -> bool:
            return await usage_rows(hot, uid) == 2

        await wait_for(both_rows, 60)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
    assert await usage_rows(hot, uid) == 2


async def test_replay_respects_the_limit_and_skips_unroutable_messages(
    kafka: str, producer: KafkaProducer
) -> None:
    key = f"limit-{uuid4()}".encode()
    for n in range(3):
        await producer.send_and_wait(
            topics.BILLING_USAGE_DLQ,
            key,
            f"body-{n}".encode(),
            {"source_topic": topics.FILE_YOUTUBE_INGESTED, "error": "x", "attempts": "5"},
        )
    await producer.send_and_wait(topics.BILLING_USAGE_DLQ, key, b"no-source", {"error": "x"})
    group = f"r-{uuid4()}"
    first = await dlq.replay(kafka, topics.BILLING_USAGE_DLQ, limit=1, group_id=group)
    assert first.replayed + first.skipped == 1
    rest = await dlq.replay(kafka, topics.BILLING_USAGE_DLQ, group_id=group)
    assert rest.replayed >= 2  # the offset moved on, so nothing is replayed twice
    again = await dlq.replay(kafka, topics.BILLING_USAGE_DLQ, group_id=group)
    assert again.replayed == 0 and again.skipped == 0
    youtube = [r for r in await read_all(kafka, topics.FILE_YOUTUBE_INGESTED) if r.key == key]
    assert len(youtube) == 3
    assert all("error" not in r.headers and "source_topic" not in r.headers for r in youtube)
