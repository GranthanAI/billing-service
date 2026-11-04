"""Replay dead-lettered messages to the topic they came from.

A message lands in a DLQ with its original key and body plus `error`, `attempts` and
`source_topic` headers. Replay sends it back to `source_topic` without the error headers, so a
missing price (or any fixed cause) is repaired by fixing it and replaying.

A replay covers only what was in the DLQ when it started (the end offsets it saw at the start).
A message that fails again is dead-lettered again *behind* that mark, so it is not replayed in a
loop, and the committed offset means the next run does not repeat this one either.
"""

from dataclasses import dataclass

import structlog
from aiokafka import AIOKafkaConsumer
from aiokafka.structs import OffsetAndMetadata

from billing.adapters.kafka.producer import KafkaProducer

log = structlog.get_logger()

DLQ_HEADERS = frozenset({"error", "attempts", "source_topic"})
BATCH = 100


@dataclass(frozen=True, slots=True)
class ReplayResult:
    replayed: int
    skipped: int  # no source_topic header: cannot be routed back


async def replay(
    bootstrap_servers: str,
    dlq_topic: str,
    *,
    limit: int | None = None,
    group_id: str | None = None,
    idle_ms: int = 3000,
) -> ReplayResult:
    group = group_id or f"billing-dlq-replay-{dlq_topic}"
    consumer = AIOKafkaConsumer(
        dlq_topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    producer = KafkaProducer(bootstrap_servers, "billing-dlq-replay")
    await consumer.start()  # joins the group: the partitions are assigned when this returns
    await producer.start()
    replayed = skipped = 0
    try:
        tps = sorted(consumer.assignment(), key=lambda tp: tp.partition)
        ends = await consumer.end_offsets(tps)  # the mark: what was in the DLQ when we began

        async def pending() -> bool:
            return any([await consumer.position(tp) < ends[tp] for tp in tps])

        while await pending() and (limit is None or replayed + skipped < limit):
            room = BATCH if limit is None else min(BATCH, limit - replayed - skipped)
            batch = await consumer.getmany(timeout_ms=idle_ms, max_records=room)
            if not batch:
                break
            for tp, records in batch.items():
                last_done = None
                for msg in records:
                    if msg.offset >= ends[tp]:
                        break  # added after we started: not ours to replay
                    headers = {k: v for k, v in (msg.headers or [])}
                    source = headers.get("source_topic", b"").decode()
                    if not source:
                        skipped += 1
                        log.warning("dlq_message_without_source", offset=msg.offset)
                    else:
                        kept = [(k, v) for k, v in (msg.headers or []) if k not in DLQ_HEADERS]
                        await producer.send_and_wait(source, msg.key, msg.value, kept)
                        replayed += 1
                    last_done = msg.offset
                if last_done is not None:
                    await consumer.commit({tp: OffsetAndMetadata(last_done + 1, "")})
                    consumer.seek(tp, last_done + 1)  # records past the mark are re-read later
    finally:
        await producer.stop()
        await consumer.stop()
    return ReplayResult(replayed, skipped)
