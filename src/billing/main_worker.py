"""Worker process: outbox relay, scheduler, job runner and Kafka consumers in one TaskGroup."""

import asyncio
import contextlib
import signal
from collections.abc import Iterator, Sequence

import structlog
import uvicorn
from fastapi import FastAPI, Response

from billing.adapters.clock import SystemClock
from billing.adapters.db import partitions
from billing.adapters.db.engine import make_engine
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.outbox_relay import OutboxRelay
from billing.adapters.kafka.producer import KafkaProducer
from billing.jobs.handlers import partitions as partitions_job
from billing.jobs.runner import JobContext, JobRunner
from billing.jobs.scheduler import Recurring, Scheduler
from billing.observability import configure_logging, metrics_payload
from billing.settings import Settings, get_settings

log = structlog.get_logger()
SCHEDULE: tuple[Recurring, ...] = (partitions_job.SCHEDULE,)
DRAIN_SECONDS = 30.0
HEALTH_PORT = 9100


def health_app() -> FastAPI:
    app = FastAPI(title="billing-worker")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = metrics_payload()
        return Response(content=body, media_type=content_type)

    return app


class _HealthServer(uvicorn.Server):
    """The worker owns SIGTERM; uvicorn must not install its own handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    consumers: Sequence[KafkaEventConsumer[object]] = (),
    health_port: int | None = HEALTH_PORT,
    drain_seconds: float = DRAIN_SECONDS,
) -> None:
    engine = make_engine(settings.billing_database_url, pool_size=settings.db_pool_size)
    ids, clock = Uuid7Generator(), SystemClock()
    producer = KafkaProducer(settings.kafka_bootstrap_servers, settings.kafka_client_id)

    def uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(engine, ids, clock)

    async with engine.begin() as conn:  # make sure this and the next months have partitions
        await partitions.ensure(conn, months_ahead=2)
    await producer.start()
    try:
        relay = OutboxRelay(engine, ids, clock, producer, batch_size=settings.outbox_batch_size)
        scheduler = Scheduler(uow_factory, clock, SCHEDULE)
        runner = JobRunner(
            JobContext(uow_factory, clock, ids), concurrency=settings.job_concurrency
        )
        health = None
        if health_port is not None:
            health = _HealthServer(
                uvicorn.Config(health_app(), host="0.0.0.0", port=health_port, log_level="warning")
            )

        async def watch_stop() -> None:
            await stop.wait()
            if health is not None:
                health.should_exit = True

        async with asyncio.TaskGroup() as group:
            group.create_task(relay.run(stop))
            group.create_task(scheduler.run(stop))
            group.create_task(runner.run(stop, drain_seconds))
            for consumer in consumers:
                group.create_task(consumer.run(stop))
            group.create_task(watch_stop())
            if health is not None:
                group.create_task(health.serve())
    finally:
        await producer.stop()
        await engine.dispose()


async def main() -> None:
    configure_logging()
    settings = get_settings()  # fails fast when the database or Kafka settings are missing
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("worker_starting")
    await run_worker(settings, stop)
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
