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
from billing.observability import CONSUMER_ERRORS, DLQ_MESSAGES

log = structlog.get_logger()

MAX_ATTEMPTS = 5
LOOP_BACKOFF_START_S = 1.0  # after an error in the consumer loop itself: 1 s, doubling,
LOOP_BACKOFF_MAX_S = 30.0  # up to 30 s, until a poll succeeds again
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
        loop_backoff: tuple[float, float] = (LOOP_BACKOFF_START_S, LOOP_BACKOFF_MAX_S),
    ) -> None:
        self._topics = topics
        self._loop_backoff = loop_backoff
        # set while the loop is failing (the exception class name); /readyz of the worker reports it
        self.failing: str | None = None
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
        DLQ_MESSAGES.labels(topic=msg.topic).inc()
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

    @property
    def topic_label(self) -> str:
        return ",".join(sorted(self._topics))[:120]

    def _record_loop_error(self, exc: BaseException) -> None:
        name = type(exc).__name__
        self.failing = name
        CONSUMER_ERRORS.labels(topic=self.topic_label, error=name).inc()
        partitions = sorted(f"{tp.topic}:{tp.partition}" for tp in self._consumer.assignment())
        log.error(
            "consumer_loop_error",
            topics=self._topics,
            group=self._group_id,
            partitions=partitions,
            error=name,
            detail=str(exc)[:300],
        )

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def _rewind_to_committed(self) -> None:
        """After a processing failure, resume from the last commit (retry it)."""
        with contextlib.suppress(Exception):
            assigned = self._consumer.assignment()
            if assigned:
                await self._consumer.seek_to_committed(*assigned)

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop`. Nothing that goes wrong in here ends the task.

        An error (fetch, decode, codec, disconnect, a failing DLQ publish or commit) is logged
        with its topic and partitions, counted in billing_consumer_errors_total{topic,error}, and
        retried from the same position after 1 s doubling to 30 s. Per-message problems (bad JSON,
        unknown schema or model) still go to the DLQ exactly as before."""
        delay = self._loop_backoff[0]
        started = False
        try:
            while not stop.is_set():
                try:
                    if not started:
                        await self._consumer.start()
                        started = True
                    batch = await self._consumer.getmany(timeout_ms=500, max_records=100)
                    if batch:
                        # in-flight messages are always finished and committed before the next poll,
                        # so a rebalance or stop never leaves a half-done message behind
                        await asyncio.gather(*(self._drain_partition(r) for r in batch.values()))
                    if self.failing is not None:
                        log.info(
                            "consumer_loop_recovered", topics=self._topics, group=self._group_id
                        )
                        self.failing = None
                    delay = self._loop_backoff[0]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_loop_error(exc)
                    if started:
                        await self._rewind_to_committed()
                    await self._sleep_or_stop(stop, delay)
                    delay = min(delay * 2, self._loop_backoff[1])
        finally:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
