"""Helpers for the Kafka integration tests."""

import time
from dataclasses import dataclass

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic


@dataclass(frozen=True)
class Rec:
    key: bytes | None
    value: bytes
    headers: dict[str, bytes]
    partition: int
    offset: int


CREATED_TOPICS: set[str] = set()


async def delete_created_topics(bootstrap: str) -> None:
    """Remove the topics this process created (never anything that was already there)."""
    if not CREATED_TOPICS:
        return
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        await admin.delete_topics(sorted(CREATED_TOPICS))
    except Exception:  # best effort: a leftover topic is harmless
        pass
    finally:
        await admin.close()
        CREATED_TOPICS.clear()


async def make_topic(bootstrap: str, name: str, partitions: int = 3) -> str:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        if name not in existing:
            await admin.create_topics([NewTopic(name, partitions, 1)])
            CREATED_TOPICS.add(name)  # this run made it, so this run removes it
    finally:
        await admin.close()
    return name


async def _partitions(bootstrap: str, topic: str) -> list[int]:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        (info,) = await admin.describe_topics([topic])
        return sorted(p["partition"] for p in info["partitions"])
    finally:
        await admin.close()


async def read_all(
    bootstrap: str, topic: str, quiet: float = 1.5, timeout: float = 30.0
) -> list[Rec]:
    """Everything currently in the topic, read without a group, partition by partition."""
    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    await consumer.start()
    out: list[Rec] = []
    try:
        tps = [TopicPartition(topic, p) for p in await _partitions(bootstrap, topic)]
        consumer.assign(tps)
        await consumer.seek_to_beginning(*tps)
        deadline = time.monotonic() + timeout
        last = time.monotonic()
        while time.monotonic() < deadline and time.monotonic() - last < quiet:
            batch = await consumer.getmany(timeout_ms=300)
            for records in batch.values():
                for r in records:
                    out.append(
                        Rec(
                            r.key,
                            r.value,
                            {k: v for k, v in r.headers or []},
                            r.partition,
                            r.offset,
                        )
                    )
                    last = time.monotonic()
    finally:
        await consumer.stop()
    return out


async def committed_offsets(bootstrap: str, group: str, topic: str) -> dict[int, int]:
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap, group_id=group, enable_auto_commit=False
    )
    await consumer.start()
    try:
        parts = await _partitions(bootstrap, topic)
        result: dict[int, int] = {}
        for p in sorted(parts):
            offset = await consumer.committed(TopicPartition(topic, p))
            if offset is not None:
                result[p] = offset
        return result
    finally:
        await consumer.stop()


from tests.support.wait import wait_until as wait_until  # noqa: E402,F401  (re-exported)
