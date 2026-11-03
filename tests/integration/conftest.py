"""Postgres 17 via Testcontainers: roles.sql, then alembic upgrade head as the migrator."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
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
from tests.support import plans

if TYPE_CHECKING:
    from tests.integration.billing_env import Env

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Urls:
    host: str
    port: int

    def _url(self, user: str, password: str, db: str) -> str:
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{db}"

    @property
    def superuser(self) -> str:
        return self._url("postgres", "postgres", "postgres")

    @property
    def migrator(self) -> str:
        return self._url("billing_migrator", "billing_migrator", "billing_db")

    @property
    def app(self) -> str:
        return self._url("billing_app", "billing_app", "billing_db")

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
                database="billing_db",
            )
            await conn.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.5)


@pytest.fixture(scope="session")
def urls() -> Iterator[Urls]:
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
    name = f"cat_{uuid4().hex[:10]}"
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
    """A single-node KRaft apache/kafka:3.8.0 whose advertised address is its mapped port."""
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
                await asyncio.sleep(0.3)
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    """Valkey 8 configured the way billing requires: AOF on, noeviction."""
    with _start_valkey("noeviction") as c:
        url = f"redis://{c.get_container_host_ip()}:{c.get_exposed_port(6379)}/0"
        asyncio.run(_wait_valkey(url))
        yield url


@pytest.fixture(scope="session")
def valkey_lru_url() -> Iterator[str]:
    """The unsafe configuration /readyz must refuse."""
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
def hot_db_name(urls: Urls) -> str:
    from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
    from billing.adapters.catalog.yaml_loader import load_catalog
    from billing.adapters.clock import SystemClock
    from billing.adapters.gateway_fake import InMemoryGateway
    from billing.app.catalog import sync_catalog

    name = f"hot_{uuid4().hex[:8]}"

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
    return name


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
def phase6_template(urls: Urls) -> Template:
    """A migrated database with prices and the real catalog synced, cloned for each test."""
    from billing.adapters.catalog.seed_loader import load_fx_rates, load_model_prices
    from billing.adapters.catalog.yaml_loader import load_catalog
    from billing.adapters.clock import SystemClock
    from billing.adapters.gateway_fake import InMemoryGateway
    from billing.app.catalog import sync_catalog

    name = f"tpl_{uuid4().hex[:8]}"
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
    return Template(name, dict(gateway.plans))


@pytest_asyncio.fixture
async def env(
    urls: Urls, phase6_template: Template, vk: valkey_asyncio.Valkey
) -> AsyncIterator[Env]:
    from tests.integration.billing_env import build_env

    name = f"p6_{uuid4().hex[:10]}"
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
