# ruff: noqa: E501
"""Billing's consumer decodes an lz4-compressed message (llm-service's producers compress with lz4).

Runs against the SHARED dev Kafka (127.0.0.1:9092, or DEV_KAFKA_BOOTSTRAP): a unique topic and
consumer group are created and deleted again. Skipped when that Kafka is not reachable.
"""

import asyncio
import contextlib
import os
import uuid
from unittest.mock import MagicMock

import pytest
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from billing.adapters.kafka.consumer import KafkaEventConsumer

BOOTSTRAP = os.environ.get("DEV_KAFKA_BOOTSTRAP", "127.0.0.1:9092")


async def _admin() -> AIOKafkaAdminClient:
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await asyncio.wait_for(admin.start(), 15)
    return admin


async def test_the_consumer_decodes_lz4_and_the_other_codecs() -> None:
    try:
        admin = await _admin()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"shared dev Kafka not reachable at {BOOTSTRAP}: {type(exc).__name__}")
    suffix = uuid.uuid4().hex[:8]
    topic, group = f"billing.test.lz4.{suffix}", f"billing-test-lz4-{suffix}"
    try:
        await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        sent = {}
        for codec in ("lz4", "gzip", "snappy", "zstd"):
            producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, compression_type=codec)
            await producer.start()
            try:
                body = (f'{{"codec": "{codec}", "padding": "' + "x" * 400 + '"}').encode()
                await producer.send_and_wait(topic, body, key=codec.encode())
                sent[codec] = body
            finally:
                await producer.stop()

        received: dict[str, bytes] = {}
        stop = asyncio.Event()

        async def handler(event: bytes) -> None:
            received[event.decode().split('"')[3]] = event
            if len(received) == len(sent):
                stop.set()

        consumer = KafkaEventConsumer(
            bootstrap_servers=BOOTSTRAP, client_id="billing-test-lz4", topics=[topic], group_id=group,
            decoder=lambda t, b: b, handler=handler, dlq_topic=f"{topic}.dlq", producer=MagicMock(),
            loop_backoff=(0.05, 0.2),
        )  # fmt: skip
        await asyncio.wait_for(consumer.run(stop), 60)

        assert received == sent  # every message, whatever its codec, decoded to its exact bytes
        assert consumer.failing is None
    finally:
        with contextlib.suppress(Exception):
            await admin.delete_consumer_groups([group])
        with contextlib.suppress(Exception):
            await admin.delete_topics([topic])
        await admin.close()
