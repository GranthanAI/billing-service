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
from billing.adapters.gateways import make_gateway
from billing.adapters.ids import Uuid7Generator
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.org_consumer import make_org_consumer
from billing.adapters.kafka.outbox_relay import OutboxRelay
from billing.adapters.kafka.producer import KafkaProducer
from billing.adapters.kafka.usage_consumer import make_usage_consumer
from billing.adapters.kafka.user_consumer import make_user_consumer
from billing.adapters.pdf import WeasyPrintRenderer
from billing.adapters.valkey.client import make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.adapters.valkey.entitlement_cache import ValkeyEntitlementCache
from billing.app.entitlements import EntitlementService
from billing.app.gauges import sample_gauges
from billing.app.invoices import InvoiceService
from billing.app.org_membership import OrgMembershipService
from billing.app.usage import UsageService
from billing.app.user_deletion import UserDeletionService
from billing.app.webhooks import EventApplier
from billing.jobs.handlers import billing as billing_jobs
from billing.jobs.handlers import finance as finance_jobs
from billing.jobs.handlers import holds as holds_job
from billing.jobs.handlers import invoices as invoice_jobs  # noqa: F401  (registers handlers)
from billing.jobs.handlers import lifecycle as lifecycle_jobs
from billing.jobs.handlers import orgs as org_jobs
from billing.jobs.handlers import partitions as partitions_job
from billing.jobs.handlers import reconcile as reconcile_jobs
from billing.jobs.handlers import usage as usage_jobs
from billing.jobs.runner import JobContext, JobRunner
from billing.jobs.scheduler import Recurring, Scheduler
from billing.jobs.webhooks import WebhookProcessor
from billing.observability import AI_BUDGET, configure_logging, init_sentry, metrics_payload
from billing.settings import Settings, get_settings, webhook_secrets
from billing.wiring import config_from_settings, make_context, make_org_directory, make_storage

log = structlog.get_logger()
SCHEDULE: tuple[Recurring, ...] = (
    partitions_job.SCHEDULE,
    holds_job.SCHEDULE,
    billing_jobs.SCHEDULE,
    usage_jobs.SCHEDULE,
    *lifecycle_jobs.SCHEDULES,
    *finance_jobs.SCHEDULES,
    reconcile_jobs.SCHEDULE,
    org_jobs.SCHEDULE,
)
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
    usage_consumer: bool = True,
    user_consumer: bool = True,
    org_consumer: bool = True,
) -> None:
    engine = make_engine(settings.billing_database_url, pool_size=settings.db_pool_size)
    ids, clock = Uuid7Generator(), SystemClock()
    producer = KafkaProducer(settings.kafka_bootstrap_servers, settings.kafka_client_id)
    valkey_client = make_valkey(settings.billing_valkey_url)
    counters = ValkeyCounters(
        valkey_client,
        day_ttl=settings.day_counter_ttl_seconds,
        month_ttl=settings.month_counter_ttl_seconds,
    )

    def uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(engine, ids, clock)

    async with engine.begin() as conn:  # make sure this and the next months have partitions
        await partitions.ensure(conn, months_ahead=2)
    gateway = make_gateway(
        settings.payment_gateway,
        settings.environment,
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        webhook_secret=webhook_secrets(settings),
        clock=clock,
    )
    org_directory = make_org_directory(settings, valkey_client)
    entitlements = EntitlementService(
        uow_factory, ValkeyEntitlementCache(valkey_client), clock, orgs=org_directory
    )
    bctx = make_context(
        config_from_settings(settings),
        uow_factory,
        gateway,
        clock,
        ids,
        ValkeyEntitlementCache(valkey_client).delete,
        entitlements.invalidate_org,
    )
    applier = EventApplier(bctx)
    storage = make_storage(settings)
    invoices = InvoiceService(uow_factory, WeasyPrintRenderer(), storage, clock, bctx.config)
    await producer.start()
    all_consumers: list[KafkaEventConsumer[object]] = list(consumers)
    if usage_consumer:
        usage = UsageService(
            uow_factory,
            entitlements,
            counters,
            clock,
            manim_render_cost_usd=settings.manim_render_cost_usd,
            youtube_ingest_cost_inr=settings.youtube_ingest_cost_inr,
        )
        all_consumers.append(
            make_usage_consumer(
                usage,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                client_id=settings.kafka_client_id,
                producer=producer,
            )  # type: ignore[arg-type]
        )
    if user_consumer:
        all_consumers.append(
            make_user_consumer(
                UserDeletionService(bctx, counters),
                bootstrap_servers=settings.kafka_bootstrap_servers,
                client_id=settings.kafka_client_id,
                producer=producer,
            )  # type: ignore[arg-type]
        )
    if org_consumer:
        all_consumers.append(
            make_org_consumer(
                OrgMembershipService(uow_factory, org_directory, entitlements.invalidate, clock),
                bootstrap_servers=settings.kafka_bootstrap_servers,
                client_id=settings.kafka_client_id,
                producer=producer,
            )  # type: ignore[arg-type]
        )
    try:
        relay = OutboxRelay(engine, ids, clock, producer, batch_size=settings.outbox_batch_size)
        scheduler = Scheduler(uow_factory, clock, SCHEDULE)
        runner = JobRunner(
            JobContext(
                uow_factory,
                clock,
                ids,
                gateway=gateway,
                counters=counters,
                billing=bctx,
                applier=applier,
                invoices=invoices,
            ),
            concurrency=settings.job_concurrency,
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
            group.create_task(WebhookProcessor(bctx, applier).run(stop))
            group.create_task(sample_gauges(uow_factory, stop))
            for consumer in all_consumers:
                group.create_task(consumer.run(stop))
            group.create_task(watch_stop())
            if health is not None:
                group.create_task(health.serve())
    finally:
        await producer.stop()
        close = getattr(gateway, "aclose", None)
        if close is not None:
            await close()
        await valkey_client.aclose()
        await engine.dispose()


async def main() -> None:
    configure_logging()
    settings = get_settings()  # fails fast when the database or Kafka settings are missing
    init_sentry(settings.sentry_dsn, settings.environment)
    AI_BUDGET.set(settings.daily_ai_budget_inr * 1_000_000)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("worker_starting")
    await run_worker(settings, stop)
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
