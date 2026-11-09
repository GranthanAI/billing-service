# ruff: noqa: E501
"""A Kafka consumer error must not stop the worker: back off, retry, report not-ready, keep the rest running."""

import asyncio
from unittest.mock import MagicMock

import pytest
from aiokafka import TopicPartition
from fastapi.testclient import TestClient

from billing.adapters.kafka import consumer as consumer_mod
from billing.adapters.kafka.compression import missing_codecs, require_codecs
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.main_worker import health_app
from billing.observability import CONSUMER_ERRORS


class UnsupportedCodecError(Exception):
    """Stands in for aiokafka.errors.UnsupportedCodecError."""


class FakeAioConsumer:
    """getmany raises the scripted errors in order, then returns empty batches; records start/stop/seek."""

    script: list[Exception] = []
    instance: "FakeAioConsumer | None" = None

    def __init__(self, *topics, **kwargs):
        self.errors = list(FakeAioConsumer.script)
        self.started = self.stopped = False
        self.sought = 0
        self.polls = 0
        FakeAioConsumer.instance = self

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def assignment(self):
        return {TopicPartition("chat.response.generated", 0)}

    async def seek_to_committed(self, *partitions):
        self.sought += 1

    async def getmany(self, timeout_ms=0, max_records=0):
        self.polls += 1
        if self.errors:
            raise self.errors.pop(0)
        await asyncio.sleep(0)
        return {}


@pytest.fixture
def make_consumer(monkeypatch):
    monkeypatch.setattr(consumer_mod, "AIOKafkaConsumer", FakeAioConsumer)

    def build(topics=("chat.response.generated",), **kw):
        FakeAioConsumer.instance = None  # each test waits for ITS consumer
        return KafkaEventConsumer(
            bootstrap_servers="x", client_id="c", topics=list(topics), group_id="g",
            decoder=lambda t, b: b, handler=lambda e: asyncio.sleep(0), dlq_topic="dlq", producer=MagicMock(),
            loop_backoff=(0.01, 0.04), **kw,
        )  # fmt: skip

    return build


def errors_total(topic: str, error: str) -> float:
    return CONSUMER_ERRORS.labels(topic=topic, error=error)._value.get()


async def test_a_codec_error_backs_off_doubling_to_the_cap_then_recovers_without_stopping_anything(
    make_consumer, monkeypatch
):
    FakeAioConsumer.script = [UnsupportedCodecError("lz4"), UnsupportedCodecError("lz4"), ConnectionError("broker"),
                              UnsupportedCodecError("lz4"), UnsupportedCodecError("lz4")]  # fmt: skip
    c = make_consumer()
    delays: list[float] = []
    real = c._sleep_or_stop

    async def record(stop, seconds):
        delays.append(seconds)
        await real(stop, 0)  # do not really wait

    monkeypatch.setattr(c, "_sleep_or_stop", record)
    before_codec = errors_total("chat.response.generated", "UnsupportedCodecError")
    before_conn = errors_total("chat.response.generated", "ConnectionError")
    stop = asyncio.Event()
    other_loop_ticks = 0
    states: list[str | None] = []

    async def other_loop():
        nonlocal other_loop_ticks
        while not stop.is_set():
            other_loop_ticks += 1  # the job runner / outbox relay stand-in
            states.append(c.failing)
            await asyncio.sleep(0)

    async def stopper():
        while FakeAioConsumer.instance is None or FakeAioConsumer.instance.polls < 8:
            await asyncio.sleep(0)
        stop.set()

    async with asyncio.TaskGroup() as group:  # the same structure the worker uses
        group.create_task(c.run(stop))
        group.create_task(other_loop())
        group.create_task(stopper())

    assert delays == [0.01, 0.02, 0.04, 0.04, 0.04]  # doubles, capped
    assert errors_total("chat.response.generated", "UnsupportedCodecError") == before_codec + 4
    assert errors_total("chat.response.generated", "ConnectionError") == before_conn + 1
    assert other_loop_ticks > 5
    assert (
        "UnsupportedCodecError" in states and states[-1] is None and c.failing is None
    )  # reported, then recovered
    fake = FakeAioConsumer.instance
    assert fake.started and fake.stopped and fake.sought == 5  # retried from the committed position


async def test_a_failing_start_is_retried_too(make_consumer):
    FakeAioConsumer.script = []
    c = make_consumer()
    attempts = {"n": 0}
    real_start = FakeAioConsumer.start

    async def flaky_start(self):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("broker not reachable yet")
        await real_start(self)

    FakeAioConsumer.script = []
    FakeAioConsumer.start = flaky_start
    try:
        stop = asyncio.Event()

        async def stopper():
            while FakeAioConsumer.instance is None or FakeAioConsumer.instance.polls < 3:
                await asyncio.sleep(0.01)
            stop.set()

        await asyncio.gather(c.run(stop), stopper())
    finally:
        FakeAioConsumer.start = real_start
    assert attempts["n"] == 3 and c.failing is None


def test_readyz_reports_a_failing_consumer_but_healthz_stays_ok():
    ok, bad = (
        MagicMock(failing=None, topic_label="a"),
        MagicMock(failing="UnsupportedCodecError", topic_label="chat.response.generated"),
    )
    app = health_app([ok, bad])
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json() == {
            "status": "not_ready",
            "failing_consumers": {"chat.response.generated": "UnsupportedCodecError"},
        }
    bad.failing = None
    with TestClient(health_app([ok, bad])) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
    with TestClient(health_app()) as client:  # no consumers: ready
        assert client.get("/readyz").status_code == 200


def test_all_four_codecs_are_available_here():
    assert missing_codecs() == []
    require_codecs()


def test_a_missing_codec_stops_the_worker_at_startup_with_a_clear_error():
    checks = {
        "gzip": lambda: True,
        "snappy": lambda: True,
        "lz4": lambda: False,
        "zstd": lambda: True,
    }
    assert missing_codecs(checks) == ["lz4"]
    with pytest.raises(RuntimeError, match=r"codec\(s\) not available: lz4.*cramjam"):
        require_codecs(checks)

    def boom() -> bool:
        raise ImportError("no cramjam")

    assert missing_codecs({"gzip": lambda: True, "zstd": boom}) == ["zstd"]


async def test_run_worker_checks_the_codecs_before_it_touches_anything(monkeypatch):
    import billing.main_worker as mw

    monkeypatch.setattr(
        mw,
        "require_codecs",
        lambda: (_ for _ in ()).throw(RuntimeError("codec(s) not available: lz4")),
    )
    settings = MagicMock(payment_simulation=False, environment="development")
    with pytest.raises(RuntimeError, match="not available: lz4"):
        await mw.run_worker(settings, asyncio.Event())
