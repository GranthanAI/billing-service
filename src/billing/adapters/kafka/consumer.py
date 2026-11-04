"""KafkaEventConsumer: at-least-once delivery, exactly-once effects.

Handler contract
----------------
* A handler opens its own unit of work and must call ``uow.processed.claim(consumer, event_id)``
  inside it. A replayed message then claims nothing and the handler returns without effect.
* The offset is committed only after the handler returned, which is after its transaction
  committed. A crash between the two redelivers the message, and ``processed_events`` makes the
  replay a no-op.
* A failing message is retried in place up to MAX_ATTEMPTS times with a growing delay; then the
  original bytes go to the dead-letter topic and the partition moves on. Messages of one
  partition are handled one at a time, in order; partitions run concurrently.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from aiokafka import AIOKafkaConsumer, ConsumerRecord, TopicPartition
from aiokafka.structs import OffsetAndMetadata

from billing.adapters.kafka.producer import KafkaProducer
from billing.app.context import correlation_id_var

log = structlog.get_logger()

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0)  # delay after attempt 1, 2, ...; capped by MAX_ATTEMPTS


class KafkaEventConsumer[T]:
    def __init__(
        self,
        *,
        bootstrap_servers: str,
        client_id: str,
        topics: list[str],
        group_id: str,
        decoder: Callable[[str, bytes], T],
        handler: Callable[[T], Awaitable[None]],
        dlq_topic: str,
        producer: KafkaProducer,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        on_dead_letter: Callable[[str], None] | None = None,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
        self._decoder = decoder
        self._handler = handler
        self._dlq = dlq_topic
        self._producer = producer
        self._backoff = backoff
        self._max_attempts = max_attempts
        self._on_dead_letter = on_dead_letter
        self._consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=bootstrap_servers,
            client_id=client_id,
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    # --- per message -------------------------------------------------------------------------

    @staticmethod
    def _headers(msg: ConsumerRecord[bytes, bytes]) -> dict[str, bytes]:
        return {k: v for k, v in (msg.headers or [])}

    async def _to_dlq(self, msg: ConsumerRecord[bytes, bytes], error: str, attempts: int) -> None:
        headers = self._headers(msg)
        headers.update(
            error=error.encode()[:1000],
            attempts=str(attempts).encode(),
            source_topic=msg.topic.encode(),
        )
        await self._producer.send_and_wait(self._dlq, msg.key, msg.value, headers)
        if self._on_dead_letter is not None:
            self._on_dead_letter(msg.topic)
        log.warning("dead_lettered", topic=msg.topic, offset=msg.offset, error=error[:200])

    async def _commit(self, msg: ConsumerRecord[bytes, bytes]) -> None:
        tp = TopicPartition(msg.topic, msg.partition)
        await self._consumer.commit({tp: OffsetAndMetadata(msg.offset + 1, "")})

    async def _process(self, msg: ConsumerRecord[bytes, bytes]) -> None:
        headers = self._headers(msg)
        correlation_id_var.set(headers.get("correlation_id", b"").decode())
        try:
            event = self._decoder(msg.topic, msg.value)
        except Exception as exc:
            log.warning("decode_failed", error=str(exc)[:200])
            await self._to_dlq(msg, "decode", 0)
            await self._commit(msg)
            return

        attempts = 0
        while True:
            attempts += 1
            try:
                await self._handler(event)
                break
            except Exception as exc:
                log.warning("handler_failed", attempt=attempts, error=str(exc)[:200])
                if attempts >= self._max_attempts:
                    await self._to_dlq(msg, str(exc) or type(exc).__name__, attempts)
                    break
                await asyncio.sleep(self._backoff[min(attempts - 1, len(self._backoff) - 1)])
        # Not inside the try above: a failing commit is a crash, and the redelivered message is
        # made harmless by processed_events.
        await self._commit(msg)

    async def _drain_partition(self, records: list[ConsumerRecord[bytes, bytes]]) -> None:
        for msg in records:
            await self._process(msg)

    # --- lifecycle ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        await self._consumer.start()
        try:
            while not stop.is_set():
                batch = await self._consumer.getmany(timeout_ms=500, max_records=100)
                if not batch:
                    continue
                # in-flight messages are always finished and committed before the next poll,
                # so a rebalance or stop never leaves a half-done message behind
                await asyncio.gather(*(self._drain_partition(r) for r in batch.values()))
        finally:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
