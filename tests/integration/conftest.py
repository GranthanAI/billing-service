"""Postgres 17 via Testcontainers: roles.sql, then alembic upgrade head as the migrator."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import valkey.asyncio as valkey_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.core.container import DockerContainer

from billing.adapters.db.engine import make_engine
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.adapters.valkey.client import aw, make_valkey
from billing.app.records import BillingAccount, PlanVersionRecord
from billing.domain.catalog import OwnerType
from tests.integration.kafka_support import delete_created_topics
from tests.support import plans

if TYPE_CHECKING:
    from tests.integration.billing_env import Env

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Urls:
    host: str
    port: int
    db: str = "billing_db"  # a per-worker database when the services are shared (TEST_PG_URL)

    def _url(self, user: str, password: str, db: str) -> str:
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{db}"

    @property
    def superuser(self) -> str:
        return self._url("postgres", "postgres", "postgres")

    @property
    def migrator(self) -> str:
        return self._url("billing_migrator", "billing_migrator", self.db)

    @property
    def app(self) -> str:
        return self._url("billing_app", "billing_app", self.db)

    def migrator_for(self, db: str) -> str:
        return self._url("billing_migrator", "billing_migrator", db)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _wait_ready(urls: Urls, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn = await asyncpg.connect(
                host=urls.host,
                port=urls.port,
                user="billing_app",
                password="billing_app",
                database=urls.db,
            )
            await conn.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.5)


RUN_ID = os.environ.get("TEST_RUN_ID") or uuid4().hex[:5]  # shared with the xdist workers
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")  # "gw0", "gw1", ... or "" without xdist
DB_PREFIXES = ("tpl", "p6", "sim", "siml", "hot", "cat", "mig", "fee", "bdb")


def new_db_name(prefix: str) -> str:
    """`<prefix>_<epoch seconds>_<run><worker>_<rand>`: unique per run and worker, and old enough
    leftovers can be told apart by the epoch (see drop_stale_databases)."""
    return f"{prefix}_{int(time.time())}_{RUN_ID}{WORKER}_{uuid4().hex[:4]}"


def valkey_db_index() -> int:
    """A database per run and worker: five slots per run (the run id picks which five), so a
    stale or second run is unlikely to share a database. Without xdist, db 0."""
    if not WORKER.startswith("gw"):
        return 0
    return 1 + (int(RUN_ID, 16) % 6) * 5 + int(WORKER.removeprefix("gw")) % 5


def _superuser_conn_args(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    return dict(
        host=parsed.hostname, port=parsed.port or 5432, user=parsed.username or "postgres",
        password=parsed.password or "postgres", database=(parsed.path or "/postgres").lstrip("/"),
    )  # fmt: skip


async def drop_stale_databases(url: str, max_age_seconds: int = 3600) -> int:
    """Drop test databases that earlier, interrupted runs left behind (older than an hour)."""
    conn = await asyncpg.connect(**_superuser_conn_args(url))  # type: ignore[arg-type]
    dropped = 0
    try:
        pattern = "^(" + "|".join(DB_PREFIXES) + ")_[0-9]{10}_"
        names = [
            r[0]
            for r in await conn.fetch("SELECT datname FROM pg_database WHERE datname ~ $1", pattern)
        ]
        for name in names:
            epoch = int(name.split("_")[1])
            if time.time() - epoch > max_age_seconds:
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                dropped += 1
    finally:
        await conn.close()
    return dropped


async def drop_database(urls: "Urls", name: str) -> None:
    """DROP the per-test / per-session database (WITH (FORCE): open connections are terminated). Never raises: a failed
    cleanup must not fail a test (drop_stale_databases sweeps leftovers at the next session start)."""
    try:
        conn = await asyncpg.connect(
            host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
        )
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="session")
def urls() -> Iterator[Urls]:
    shared = os.environ.get("TEST_PG_URL")
    if shared:  # `make test-up` services: a database of our own per run and worker
        parsed = urlparse(shared)
        host, port = parsed.hostname or "localhost", parsed.port or 5432
        name = new_db_name("bdb")

        async def create() -> None:
            await drop_stale_databases(shared)
            conn = await asyncpg.connect(**_superuser_conn_args(shared))  # type: ignore[arg-type]
            try:
                await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
            finally:
                await conn.close()

        asyncio.run(create())
        yield Urls(host, port, name)

        async def drop() -> None:
            conn = await asyncpg.connect(**_superuser_conn_args(shared))  # type: ignore[arg-type]
            try:
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            finally:
                await conn.close()

        with contextlib.suppress(Exception):
            asyncio.run(drop())
        return
    container = (
        DockerContainer("postgres:17")
        .with_env("POSTGRES_USER", "postgres")
        .with_env("POSTGRES_PASSWORD", "postgres")
        .with_env("POSTGRES_DB", "postgres")
        .with_exposed_ports(5432)
        .with_volume_mapping(
            str(ROOT / "deploy" / "sql" / "roles.sql"),
            "/docker-entrypoint-initdb.d/10-roles.sql",
            "ro",
        )
    )
    with container:
        u = Urls(container.get_container_host_ip(), int(container.get_exposed_port(5432)))
        asyncio.run(_wait_ready(u))
        yield u


@pytest.fixture(scope="session")
def migrated(urls: Urls) -> Urls:
    command.upgrade(alembic_config(urls.migrator), "head")
    return urls


@pytest_asyncio.fixture
async def app_engine(migrated: Urls) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(migrated.app, pool_size=10)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def migrator_engine(migrated: Urls) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(migrated.migrator, pool_size=5)
    yield engine
    await engine.dispose()


@pytest.fixture
def make_uow(app_engine: AsyncEngine) -> Callable[[], SqlUnitOfWork]:
    ids = Uuid7Generator()
    return lambda: SqlUnitOfWork(app_engine, ids)


@pytest.fixture
def make_migrator_uow(migrator_engine: AsyncEngine) -> Callable[[], SqlUnitOfWork]:
    ids = Uuid7Generator()
    return lambda: SqlUnitOfWork(migrator_engine, ids)


@pytest_asyncio.fixture
async def seeded_plans(make_migrator_uow: Callable[[], SqlUnitOfWork]) -> None:
    """Free, Pro and Max as plan_versions (the app role may only read them)."""
    from datetime import UTC, datetime

    async with make_migrator_uow() as uow:
        for pv in (plans.FREE, plans.PRO, plans.MAX):
            if await uow.plans.by_id(pv.id) is None:
                await uow.plans.insert_version(
                    PlanVersionRecord(pv, datetime(2026, 11, 1, tzinfo=UTC), "checksum")
                )


@pytest_asyncio.fixture
async def account(make_uow: Callable[[], SqlUnitOfWork]) -> BillingAccount:
    async with make_uow() as uow:
        return await uow.accounts.get_or_create(OwnerType.USER, uuid4(), "user@example.com")


@dataclass
class FreshDb:
    """A throwaway migrated database, so catalog tests start from an empty plan_versions."""

    urls: Urls
    name: str
    migrator_engine: AsyncEngine
    app_engine: AsyncEngine

    @property
    def app_url(self) -> str:
        return self.urls._url("billing_app", "billing_app", self.name)

    def migrator_uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self.migrator_engine, _IDS)

    def app_uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self.app_engine, _IDS)


_IDS = Uuid7Generator()


@pytest_asyncio.fixture
async def fresh_db(urls: Urls) -> AsyncIterator[FreshDb]:
    name = new_db_name("cat")
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
    finally:
        await conn.close()
    await asyncio.to_thread(command.upgrade, alembic_config(urls.migrator_for(name)), "head")
    migrator = make_engine(urls.migrator_for(name), pool_size=5)
    app = make_engine(urls._url("billing_app", "billing_app", name), pool_size=5)
    yield FreshDb(urls, name, migrator, app)
    await migrator.dispose()
    await app.dispose()
    await drop_database(urls, name)


# --- Kafka ------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


async def _wait_kafka(bootstrap: str, timeout: float = 90.0) -> None:
    from aiokafka.admin import AIOKafkaAdminClient

    deadline = time.monotonic() + timeout
    while True:
        admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
        try:
            await admin.start()
            await admin.list_topics()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1)
        finally:
            await admin.close()


@pytest.fixture(scope="session")
def kafka() -> Iterator[str]:
    """A single-node KRaft apache/kafka:3.8.0 whose advertised address is its mapped port; with
    TEST_KAFKA_BOOTSTRAP (`make test-up`) the shared broker."""
    shared = os.environ.get("TEST_KAFKA_BOOTSTRAP")
    if shared:
        asyncio.run(_wait_kafka(shared))
        yield shared
        asyncio.run(delete_created_topics(shared))  # only the topics THIS run created
        return
    port = _free_port()
    container = (
        DockerContainer("apache/kafka:3.8.0")
        .with_bind_ports(port, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", f"PLAINTEXT://:{port},CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://localhost:{port}")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env(
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT"
        )
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_INTER_BROKER_LISTENER_NAME", "PLAINTEXT")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
    )
    with container:
        bootstrap = f"localhost:{port}"
        asyncio.run(_wait_kafka(bootstrap))
        yield bootstrap


# --- Valkey ---------------------------------------------------------------------------------


def _start_valkey(policy: str) -> DockerContainer:
    return (
        DockerContainer("valkey/valkey:8")
        .with_command(f"valkey-server --appendonly yes --maxmemory-policy {policy}")
        .with_exposed_ports(6379)
    )


async def _wait_valkey(url: str, timeout: float = 30.0) -> None:
    client = make_valkey(url)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                await aw(client.ping())
                return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    """Valkey 8 configured the way billing requires: AOF on, noeviction. With TEST_VALKEY_URL
    (`make test-up`) the shared one, on this worker's own database index."""
    shared = os.environ.get("TEST_VALKEY_URL")
    if shared:
        url = f"{shared.rstrip('/')}/{valkey_db_index()}"
        asyncio.run(_wait_valkey(url))
        yield url
        return
    with _start_valkey("noeviction") as c:
        url = f"redis://{c.get_container_host_ip()}:{c.get_exposed_port(6379)}/0"
        asyncio.run(_wait_valkey(url))
        yield url


@pytest.fixture(scope="session")
def valkey_lru_url() -> Iterator[str]:
    """The unsafe configuration /readyz must refuse."""
    shared = os.environ.get("TEST_VALKEY_LRU_URL")
    if shared:
        asyncio.run(_wait_valkey(shared))
        yield shared
        return
    with _start_valkey("allkeys-lru") as c:
        url = f"redis://{c.get_container_host_ip()}:{c.get_exposed_port(6379)}/0"
        asyncio.run(_wait_valkey(url))
        yield url


@pytest_asyncio.fixture
async def vk(valkey_url: str) -> AsyncIterator[valkey_asyncio.Valkey]:
    client = make_valkey(valkey_url)
    yield client
    await client.aclose()


# --- a migrated database with the catalog synced and prices seeded -------------------------


@pytest.fixture(scope="session")
def hot_db_name(urls: Urls) -> Iterator[str]:
    from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
    from billing.adapters.catalog.yaml_loader import load_catalog
    from billing.adapters.clock import SystemClock
    from billing.adapters.gateway_fake import InMemoryGateway
    from billing.app.catalog import sync_catalog

    name = new_db_name("hot")

    async def prepare() -> None:
        conn = await asyncpg.connect(
            host=urls.host,
            port=urls.port,
            user="postgres",
            password="postgres",
            database="postgres",
        )
        try:
            await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
        finally:
            await conn.close()

    asyncio.run(prepare())
    command.upgrade(alembic_config(urls.migrator_for(name)), "head")

    async def seed() -> None:
        engine = make_engine(urls.migrator_for(name), pool_size=2)
        ids = Uuid7Generator()
        try:
            async with SqlUnitOfWork(engine, ids) as uow:
                for p in load_model_prices(ROOT / "config/seed/model_prices.yaml"):
                    await uow.pricing.add_model_price(
                        model=p.model,
                        provider=p.provider,
                        model_group=p.group,
                        input_usd_per_m=p.input_usd_per_m,
                        cached_input_usd_per_m=p.cached_input_usd_per_m,
                        output_usd_per_m=p.output_usd_per_m,
                        effective_from=p.effective_from,
                    )
                for r in load_fx_rates(ROOT / "config/seed/fx_rates.yaml"):
                    await uow.pricing.add_fx_rate(r.effective_from, r.usd_inr, r.source)
            await sync_catalog(
                load_catalog(ROOT / "config/plans.yaml"),
                lambda: SqlUnitOfWork(engine, ids),
                InMemoryGateway(),
                SystemClock(),
                ids,
            )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    yield name
    asyncio.run(drop_database(urls, name))


@pytest_asyncio.fixture
async def hot(urls: Urls, hot_db_name: str) -> AsyncIterator[FreshDb]:
    migrator = make_engine(urls.migrator_for(hot_db_name), pool_size=3)
    app = make_engine(urls._url("billing_app", "billing_app", hot_db_name), pool_size=10)
    yield FreshDb(urls, hot_db_name, migrator, app)
    await migrator.dispose()
    await app.dispose()


# --- Phase 6: one cloned database per test ------------------------------------------------------


@dataclass(frozen=True)
class Template:
    name: str
    plans: dict[str, object]  # razorpay plan id -> GatewayPlanSpec, for the fake gateway


@pytest.fixture(scope="session")
def phase6_template(urls: Urls) -> Iterator[Template]:
    """A migrated database with prices and the real catalog synced, cloned for each test."""
    from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
    from billing.adapters.catalog.yaml_loader import load_catalog
    from billing.adapters.clock import SystemClock
    from billing.adapters.gateway_fake import InMemoryGateway
    from billing.app.catalog import sync_catalog

    name = new_db_name("tpl")
    gateway = InMemoryGateway()

    async def prepare() -> None:
        conn = await asyncpg.connect(
            host=urls.host,
            port=urls.port,
            user="postgres",
            password="postgres",
            database="postgres",
        )
        try:
            await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
        finally:
            await conn.close()

    asyncio.run(prepare())
    command.upgrade(alembic_config(urls.migrator_for(name)), "head")

    async def seed() -> None:
        engine = make_engine(urls.migrator_for(name), pool_size=2)
        ids = Uuid7Generator()
        try:
            async with SqlUnitOfWork(engine, ids) as uow:
                for p in load_model_prices(ROOT / "config/seed/model_prices.yaml"):
                    await uow.pricing.add_model_price(
                        model=p.model,
                        provider=p.provider,
                        model_group=p.group,
                        input_usd_per_m=p.input_usd_per_m,
                        cached_input_usd_per_m=p.cached_input_usd_per_m,
                        output_usd_per_m=p.output_usd_per_m,
                        effective_from=p.effective_from,
                    )
                for r in load_fx_rates(ROOT / "config/seed/fx_rates.yaml"):
                    await uow.pricing.add_fx_rate(r.effective_from, r.usd_inr, r.source)
            await sync_catalog(
                load_catalog(ROOT / "config/plans.yaml"),
                lambda: SqlUnitOfWork(engine, ids),
                gateway,
                SystemClock(),
                ids,
            )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    yield Template(name, dict(gateway.plans))
    asyncio.run(drop_database(urls, name))     # the clones are dropped by their own fixtures; the template goes last


@pytest_asyncio.fixture
async def env(
    urls: Urls, phase6_template: Template, vk: valkey_asyncio.Valkey
) -> AsyncIterator[Env]:
    from tests.integration.billing_env import build_env

    await vk.flushdb()  # counters from earlier tests would show up in reconciliation
    name = new_db_name("p6")
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(
            f'CREATE DATABASE "{name}" TEMPLATE "{phase6_template.name}" OWNER billing_migrator'
        )
    finally:
        await conn.close()
    app = make_engine(urls._url("billing_app", "billing_app", name), pool_size=10)
    migrator = make_engine(urls.migrator_for(name), pool_size=2)
    built = build_env(app, migrator, vk, phase6_template.plans)
    yield built
    await app.dispose()
    await migrator.dispose()
    await drop_database(urls, name)


@pytest_asyncio.fixture
async def sim_env(
    urls: Urls, phase6_template: Template, vk: valkey_asyncio.Valkey
) -> AsyncIterator[Env]:
    """Like `env`, but the payment gateway is the Postgres-backed SimulatorGateway (time is the
    test clock; webhooks go out immediately and are delivered with `pump_webhooks`)."""
    from billing.adapters.gateway_sim import SimConfig, SimulatorGateway
    from tests.integration.billing_env import build_env, config, seed_sim_plans

    await vk.flushdb()
    name = new_db_name("sim")
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(
            f'CREATE DATABASE "{name}" TEMPLATE "{phase6_template.name}" OWNER billing_migrator'
        )
    finally:
        await conn.close()
    app = make_engine(urls._url("billing_app", "billing_app", name), pool_size=10)
    migrator = make_engine(urls.migrator_for(name), pool_size=2)
    await seed_sim_plans(app, phase6_template.plans)

    def make_sim(clock: object) -> object:
        return SimulatorGateway(
            app,
            key_id="rzp_sim_key",
            key_secret="sim_key_secret",
            webhook_secret="sim_webhook_secret",
            clock=clock,  # type: ignore[arg-type]
            config=SimConfig(webhook_delay=0, refund_delay=10),
        )

    cfg = config(
        simulation=True, invoice_series="SI", bill_of_supply_series="SB", credit_note_series="SC"
    )
    built = build_env(app, migrator, vk, phase6_template.plans, cfg, make_gateway=make_sim)  # type: ignore[arg-type]
    yield built
    await app.dispose()
    await migrator.dispose()
    await drop_database(urls, name)


@pytest_asyncio.fixture
async def sim_lifecycle_env(
    request: pytest.FixtureRequest,
    urls: Urls,
    phase6_template: Template,
    vk: valkey_asyncio.Valkey,
) -> AsyncIterator[Env]:
    """`env` with the simulator (SIM_MONTH_SECONDS=60) as the gateway, scripted through SimDriver
    so the lifecycle tests written for the fake gateway run unchanged."""
    from billing.adapters.gateway_sim import SimConfig
    from tests.integration.billing_env import build_env, config, seed_sim_plans
    from tests.support.sim_driver import SimDriver

    await vk.flushdb()
    name = new_db_name("siml")
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(
            f'CREATE DATABASE "{name}" TEMPLATE "{phase6_template.name}" OWNER billing_migrator'
        )
    finally:
        await conn.close()
    url = urls._url("billing_app", "billing_app", name)
    app = make_engine(url, pool_size=10)
    migrator = make_engine(urls.migrator_for(name), pool_size=2)
    await seed_sim_plans(app, phase6_template.plans)
    drivers: list[SimDriver] = []
    # SIM_MONTH_SECONDS=60, unless the test module says the test needs calendar months
    month_seconds = getattr(request.module, "month_seconds_for", lambda _name: 60)(
        request.node.originalname
    )

    def make_driver(clock: object) -> object:
        driver = SimDriver(
            app,
            url,
            clock=clock,  # type: ignore[arg-type]
            config=SimConfig(month_seconds=month_seconds, webhook_delay=0),
        )
        drivers.append(driver)
        return driver

    cfg = config(
        simulation=True, invoice_series="SI", bill_of_supply_series="SB", credit_note_series="SC"
    )
    built = build_env(app, migrator, vk, phase6_template.plans, cfg, make_gateway=make_driver)  # type: ignore[arg-type]
    yield built
    for driver in drivers:
        driver.close()
    await app.dispose()
    await migrator.dispose()
    await drop_database(urls, name)
