import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa

from billing.adapters.clock import SystemClock
from billing.adapters.ids import Uuid7Generator
from billing.adapters.kafka.codec import decode_event
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.outbox_relay import OutboxRelay
from billing.adapters.kafka.producer import KafkaProducer
from billing.app import topics
from billing.app.events import PaymentFailedEvent
from billing.domain.money import Paise
from tests.integration.conftest import FreshDb
from tests.integration.kafka_support import committed_offsets, make_topic, read_all, wait_until

IDS = Uuid7Generator()


@pytest.fixture(scope="session")
def all_topics(kafka: str) -> Iterator[None]:
    async def create() -> None:
        for t in topics.ALL:
            await make_topic(kafka, t, partitions=6)

    asyncio.run(create())
    yield


@pytest_asyncio.fixture
async def producer(kafka: str, all_topics: None) -> AsyncIterator[KafkaProducer]:
    p = KafkaProducer(kafka, "test")
    await p.start()
    yield p
    await p.stop()


def failed_event(account: UUID, n: int = 0) -> PaymentFailedEvent:
    return PaymentFailedEvent(
        account, uuid4(), f"pay_{n}_{uuid4().hex[:8]}", Paise(49900), "x", None
    )


def relay_for(db: FreshDb, producer: KafkaProducer, **kw: object) -> OutboxRelay:
    return OutboxRelay(
        db.app_engine, IDS, SystemClock(), producer, idle_sleep=0.05, lock_retry=0.2, **kw
    )  # type: ignore[arg-type]


async def mine(kafka: str, account: UUID) -> list[PaymentFailedEvent]:
    out = []
    for rec in await read_all(kafka, topics.PAYMENT_FAILED):
        if rec.key == str(account).encode():
            _, event = decode_event(topics.PAYMENT_FAILED, rec.value)
            assert isinstance(event, PaymentFailedEvent)
            out.append(event)
    return out


async def unpublished(db: FreshDb) -> int:
    async with db.app_uow() as uow:
        return (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.outbox WHERE published_at IS NULL")
            )
        ).scalar_one()


# --- outbox -------------------------------------------------------------------------------------


async def test_outbox_publishes_in_id_order_and_marks_rows(
    fresh_db: FreshDb, producer: KafkaProducer, kafka: str
) -> None:
    account = uuid4()
    events = [failed_event(account, i) for i in range(10)]
    async with fresh_db.app_uow() as uow:
        for e in events:
            await uow.publisher.enqueue(e)
    assert await unpublished(fresh_db) == 10

    assert await relay_for(fresh_db, producer).relay_once() == 10
    assert await unpublished(fresh_db) == 0
    assert await mine(kafka, account) == events  # same key, same partition, same order
    async with fresh_db.app_uow() as uow:
        rows = await uow.connection.execute(
            sa.text("SELECT headers FROM billing.outbox ORDER BY id")
        )
        assert all({"event_id", "event_type"} <= set(r.headers) for r in rows)


async def test_rolled_back_enqueue_publishes_nothing(
    fresh_db: FreshDb, producer: KafkaProducer, kafka: str
) -> None:
    account = uuid4()
    with pytest.raises(RuntimeError):
        async with fresh_db.app_uow() as uow:
            await uow.publisher.enqueue(failed_event(account))
            raise RuntimeError("boom")
    assert await relay_for(fresh_db, producer).relay_once() == 0
    assert await mine(kafka, account) == []


async def test_relay_crash_then_restart_gives_exactly_one_effect(
    fresh_db: FreshDb, producer: KafkaProducer, kafka: str
) -> None:
    """The exit check: duplicate delivery, single effect."""
    account = uuid4()
    event = failed_event(account)
    async with fresh_db.app_uow() as uow:  # a use case writes state and the event together
        await uow.publisher.enqueue(event)

    async def crash() -> None:
        raise RuntimeError("relay crashed after flush, before published_at")

    with pytest.raises(RuntimeError, match="crashed"):
        await relay_for(fresh_db, producer, after_flush=crash).relay_once()
    assert await unpublished(fresh_db) == 1  # the update never happened

    assert await relay_for(fresh_db, producer).relay_once() == 1  # restarted relay
    delivered = await mine(kafka, account)
    assert delivered == [event, event]  # produced twice

    calls: list[str] = []

    async def handler(decoded: tuple[object, object]) -> None:
        envelope, ev = decoded
        assert isinstance(ev, PaymentFailedEvent)
        if ev.billing_account_id != account:
            return  # another test's event on the shared topic
        event_id = envelope.event_id  # type: ignore[attr-defined]
        calls.append(event_id)
        async with fresh_db.app_uow() as uow:
            if not await uow.processed.claim("t-exactly-once", event_id):
                return
            await uow.audit.record(UUID(int=1), "effect", "payment", event_id, "t", None, None)

    consumer = KafkaEventConsumer(
        bootstrap_servers=kafka,
        client_id="t",
        topics=[topics.PAYMENT_FAILED],
        group_id=f"g-{uuid4()}",
        decoder=lambda b: decode_event(topics.PAYMENT_FAILED, b),
        handler=handler,
        dlq_topic=topics.BILLING_USER_DLQ,
        producer=producer,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await wait_until(lambda: len(calls) >= 2, 40)  # both copies reached the handler
    finally:
        stop.set()
        await task
    assert len(set(calls)) == 1  # the same event twice
    async with fresh_db.app_uow() as uow:
        effects = (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.admin_audit WHERE target_id = :e"),
                {"e": calls[0]},
            )
        ).scalar_one()
    assert effects == 1


# --- consumer -----------------------------------------------------------------------------------


class Harness:
    """A throwaway topic, DLQ and consumer group plus a handler that records in the database."""

    def __init__(self, kafka: str, db: FreshDb, producer: KafkaProducer) -> None:
        self.kafka, self.db, self.producer = kafka, db, producer
        self.topic = f"t-{uuid4().hex[:8]}"
        self.dlq = f"{self.topic}.dlq"
        self.group = f"g-{uuid4().hex[:8]}"
        self.calls: dict[str, int] = {}
        self.fail_first: dict[str, int] = {}

    async def setup(self) -> "Harness":
        await make_topic(self.kafka, self.topic, 3)
        await make_topic(self.kafka, self.dlq, 1)
        return self

    async def handler(self, value: str) -> None:
        self.calls[value] = self.calls.get(value, 0) + 1
        async with self.db.app_uow() as uow:
            if not await uow.processed.claim(self.group, value):
                return
            if value.startswith("bad") or self.calls[value] <= self.fail_first.get(value, 0):
                raise RuntimeError(f"handler failed for {value}")  # rolls the claim back
            await uow.audit.record(UUID(int=1), "effect", "msg", value, "t", None, None)

    def consumer(
        self, cls: type[KafkaEventConsumer[str]] = KafkaEventConsumer
    ) -> KafkaEventConsumer[str]:
        def decode(b: bytes) -> str:
            if b.startswith(b"\xff"):
                raise ValueError("malformed")
            return b.decode()

        return cls(
            bootstrap_servers=self.kafka,
            client_id="t",
            topics=[self.topic],
            group_id=self.group,
            decoder=decode,
            handler=self.handler,
            dlq_topic=self.dlq,
            producer=self.producer,
            backoff=(0.01,) * 5,
        )

    async def send(self, value: bytes, key: bytes = b"k") -> None:
        await self.producer.send_and_wait(self.topic, key, value, {"correlation_id": "c-test"})

    async def effects(self, value: str) -> int:
        async with self.db.app_uow() as uow:
            return (
                await uow.connection.execute(
                    sa.text("SELECT count(*) FROM billing.admin_audit WHERE target_id = :v"),
                    {"v": value},
                )
            ).scalar_one()

    async def run_until(self, consumer: KafkaEventConsumer[str], cond: Callable[[], bool]) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(consumer.run(stop))
        try:
            await wait_until(cond, 40)
            await asyncio.sleep(0.5)  # let the commit land
        finally:
            stop.set()
            await task


@pytest_asyncio.fixture
async def harness(kafka: str, fresh_db: FreshDb, producer: KafkaProducer) -> Harness:
    return await Harness(kafka, fresh_db, producer).setup()


async def test_handler_failing_twice_then_succeeding_applies_once(harness: Harness) -> None:
    harness.fail_first["m1"] = 2
    await harness.send(b"m1")
    await harness.run_until(harness.consumer(), lambda: harness.calls.get("m1", 0) >= 3)
    assert harness.calls["m1"] == 3
    assert await harness.effects("m1") == 1
    assert sum((await committed_offsets(harness.kafka, harness.group, harness.topic)).values()) == 1
    assert await read_all(harness.kafka, harness.dlq) == []


async def test_poison_message_goes_to_dlq_and_the_partition_moves_on(harness: Harness) -> None:
    await harness.send(b"bad-1")
    await harness.send(b"ok-1")  # same key, same partition: behind the poison message
    await harness.run_until(harness.consumer(), lambda: harness.calls.get("ok-1", 0) >= 1)
    assert harness.calls["bad-1"] == 5
    assert await harness.effects("ok-1") == 1
    (dead,) = await read_all(harness.kafka, harness.dlq)
    assert dead.value == b"bad-1" and dead.key == b"k"
    assert dead.headers["attempts"] == b"5"
    assert dead.headers["source_topic"] == harness.topic.encode()
    assert b"handler failed" in dead.headers["error"]
    assert dead.headers["correlation_id"] == b"c-test"  # original headers are kept
    assert sum((await committed_offsets(harness.kafka, harness.group, harness.topic)).values()) == 2


async def test_malformed_bytes_go_straight_to_the_dlq(harness: Harness) -> None:
    await harness.send(b"\xff\xfe-not-protobuf")
    await harness.send(b"after")
    await harness.run_until(harness.consumer(), lambda: harness.calls.get("after", 0) >= 1)
    (dead,) = await read_all(harness.kafka, harness.dlq)
    assert dead.headers["error"] == b"decode" and dead.headers["attempts"] == b"0"
    assert dead.value == b"\xff\xfe-not-protobuf"
    assert sum((await committed_offsets(harness.kafka, harness.group, harness.topic)).values()) == 2


async def test_crash_between_handler_commit_and_offset_commit_is_harmless(
    harness: Harness,
) -> None:
    class CrashOnce(KafkaEventConsumer[str]):
        crashed = False

        async def _commit(self, msg):  # type: ignore[no-untyped-def]
            if not CrashOnce.crashed:
                CrashOnce.crashed = True
                raise RuntimeError("crash before the offset commit")
            await super()._commit(msg)

    await harness.send(b"m8")
    first = harness.consumer(CrashOnce)
    with pytest.raises(RuntimeError, match="crash before"):
        await asyncio.wait_for(first.run(asyncio.Event()), 40)
    assert harness.calls["m8"] == 1 and await harness.effects("m8") == 1
    assert await committed_offsets(harness.kafka, harness.group, harness.topic) == {}

    await harness.run_until(harness.consumer(), lambda: harness.calls["m8"] >= 2)  # redelivered
    assert harness.calls["m8"] == 2
    assert await harness.effects("m8") == 1  # processed_events made the replay a no-op
    assert sum((await committed_offsets(harness.kafka, harness.group, harness.topic)).values()) == 1


# --- relay lock ---------------------------------------------------------------------------------


async def test_only_one_relay_holds_the_lock_and_another_takes_over(
    fresh_db: FreshDb, producer: KafkaProducer
) -> None:
    a, b = relay_for(fresh_db, producer), relay_for(fresh_db, producer)
    stop_a, stop_b = asyncio.Event(), asyncio.Event()
    ta, tb = asyncio.create_task(a.run(stop_a)), asyncio.create_task(b.run(stop_b))
    try:
        await wait_until(lambda: a.holds_lock or b.holds_lock, 10)
        await asyncio.sleep(1)
        assert a.holds_lock != b.holds_lock  # exactly one
        holder, other, stop_holder = (a, b, stop_a) if a.holds_lock else (b, a, stop_b)
        stop_holder.set()
        await wait_until(lambda: other.holds_lock and not holder.holds_lock, 10)
    finally:
        stop_a.set()
        stop_b.set()
        await asyncio.gather(ta, tb)


async def test_relay_run_loop_publishes_new_rows(
    fresh_db: FreshDb, producer: KafkaProducer, kafka: str
) -> None:
    account = uuid4()
    relay = relay_for(fresh_db, producer)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop))
    try:
        async with fresh_db.app_uow() as uow:
            await uow.publisher.enqueue(failed_event(account))
        await wait_until(lambda: relay.holds_lock, 10)
        await wait_until(lambda: drained(fresh_db), 20)
    finally:
        stop.set()
        await task
    assert len(await mine(kafka, account)) == 1


async def drained(db: FreshDb) -> bool:
    return await unpublished(db) == 0
