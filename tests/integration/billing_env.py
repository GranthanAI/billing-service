"""A complete Phase 6 world for one test: its own database (cloned from a template), a test
clock, the scripted fake gateway, the use cases, the webhook processor and the job runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.adapters.pdf import WeasyPrintRenderer
from billing.adapters.storage import InMemoryStorage
from billing.adapters.valkey.counters import ValkeyCounters
from billing.adapters.valkey.entitlement_cache import ValkeyEntitlementCache
from billing.app.account_views import AccountViews
from billing.app.admin import AdminService
from billing.app.config import BillingConfig, BillingContext
from billing.app.entitlements import EntitlementService
from billing.app.invoices import InvoiceService
from billing.app.settlements import SettlementService
from billing.app.subscriptions import CheckoutOptions, SubscriptionService
from billing.app.webhooks import EventApplier
from billing.domain.catalog import Interval
from billing.domain.entitlements import Entitlement
from billing.jobs.handlers import billing as billing_jobs  # noqa: F401  (registers handlers)
from billing.jobs.handlers import finance as finance_jobs  # noqa: F401
from billing.jobs.handlers import invoices as invoice_jobs  # noqa: F401
from billing.jobs.handlers import lifecycle as lifecycle_jobs  # noqa: F401
from billing.jobs.handlers import reconcile as reconcile_jobs  # noqa: F401
from billing.jobs.runner import JobContext, JobRunner
from billing.jobs.webhooks import WebhookProcessor
from billing.main_api import app
from billing.wiring import install_billing
from tests.support.clock import TestClock
from tests.support.fake_gateway import FakeGateway, Webhook

START = datetime(2026, 11, 1, 4, 45, tzinfo=UTC)  # 10:15 IST on 1 Nov 2026: financial year 2627
STATE_BIHAR = "10"
PRO_MONTH = 49900
MAX_MONTH = 149900
GATEWAY_SECRET = "test-gateway-secret"


def config(**overrides: object) -> BillingConfig:
    base: dict[str, object] = dict(
        razorpay_key_id="rzp_test_key",
        supplier_state_code="10",
        supplier_gstin="10AAAAA0000A1Z5",
    )
    base.update(overrides)
    return BillingConfig(**base)  # type: ignore[arg-type]


@dataclass
class Env:
    app_engine: AsyncEngine
    migrator_engine: AsyncEngine
    clock: TestClock
    gateway: FakeGateway
    bctx: BillingContext
    applier: EventApplier
    service: SubscriptionService
    processor: WebhookProcessor
    runner: JobRunner
    entitlements: EntitlementService
    storage: InMemoryStorage
    counters: ValkeyCounters
    invoices: InvoiceService
    invalidated: list[UUID] = field(default_factory=list)
    user: UUID = field(default_factory=uuid4)
    _job_seq: int = 0

    def uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self.app_engine, self.bctx.ids, self.clock)

    async def sql(self, query: str, **params: object) -> list[sa.Row[tuple[object, ...]]]:
        async with self.app_engine.connect() as conn:
            return list((await conn.execute(sa.text(query), params)).all())

    async def scalar(self, query: str, **params: object) -> object:
        rows = await self.sql(query, **params)
        return rows[0][0] if rows else None

    async def deliver(self, *hooks: Webhook) -> None:
        """What the webhook route does: store the delivery, nothing else."""
        async with self.uow() as uow:
            for h in hooks:
                await uow.inbox.store("razorpay", h.event_id, h.event_type, h.payload())

    async def process(self) -> int:
        total = 0
        while claimed := await self.processor.process_pending():
            total += claimed
        return total

    async def run_jobs(self) -> int:
        return await self.runner.run_pending()

    async def run_job(self, kind: str, payload: dict[str, object] | None = None) -> None:
        """Enqueue one job of `kind` now and run everything that is ready."""
        self._job_seq += 1
        async with self.uow() as uow:
            await uow.jobs.enqueue(
                kind, payload or {}, self.clock.now(), f"{kind}:{self._job_seq}:{self.clock.now()}"
            )
        await self.runner.run_pending()

    def set_period(self, period: str) -> None:
        """Give every fake plan a billing period such as "10days" (fake-only)."""
        for plan_id, spec in list(self.gateway.plans.items()):
            self.gateway.plans[plan_id] = replace(spec, period=period)

    async def checkout(
        self, plan: str = "pro", interval: Interval = Interval.MONTH, user: UUID | None = None
    ) -> CheckoutOptions:
        return await self.service.checkout(
            user or self.user, plan, interval, STATE_BIHAR, "user@example.com"
        )

    async def activate(
        self, plan: str = "pro", amount: int = PRO_MONTH, interval: Interval = Interval.MONTH
    ) -> CheckoutOptions:
        """Checkout, the user pays, both webhooks arrive and are processed."""
        options = await self.checkout(plan, interval)
        authenticated, charged, _, _ = self.gateway.pay_checkout(
            options.gateway_subscription_id, amount
        )
        await self.deliver(authenticated, charged)
        await self.process()
        return options

    async def status(self, user: UUID | None = None) -> str | None:
        rows = await self.sql(
            "SELECT s.status::text FROM billing.subscriptions s "
            "JOIN billing.billing_accounts a ON a.id = s.billing_account_id "
            "WHERE a.owner_id = :u ORDER BY s.created_at DESC LIMIT 1",
            u=user or self.user,
        )
        return str(rows[0][0]) if rows else None

    async def plan_code(self, user: UUID | None = None) -> str:
        ent: Entitlement = await self.entitlements.resolve(user or self.user)
        return ent.grant.plan_version.plan_code

    def with_config(self, cfg: BillingConfig) -> None:
        """Swap the settings-derived configuration (GST_REGISTERED and friends)."""
        self.bctx = replace(self.bctx, config=cfg)
        self.applier = EventApplier(self.bctx)
        self.service = SubscriptionService(self.bctx, self.applier)
        self.processor = WebhookProcessor(self.bctx, self.applier)
        self.invoices = InvoiceService(
            self.bctx.uow_factory, WeasyPrintRenderer(), self.storage, self.clock, cfg
        )
        self.runner = JobRunner(
            JobContext(
                self.bctx.uow_factory,
                self.clock,
                self.bctx.ids,
                gateway=self.gateway,
                billing=self.bctx,
                applier=self.applier,
                invoices=self.invoices,
                counters=self.counters,
            ),
            poll_interval=0.01,
        )

    def api(self) -> httpx.AsyncClient:
        install_billing(
            app,
            self.bctx,
            views=AccountViews(
                self.bctx.uow_factory, self.entitlements, self.counters, self.storage, self.clock
            ),
            admin=AdminService(
                self.bctx.uow_factory,
                self.clock,
                self.bctx.ids,
                SettlementService(self.bctx.uow_factory, self.gateway, self.clock),
                self.entitlements.invalidate,
            ),
        )
        app.state.gateway_secrets = (GATEWAY_SECRET,)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-Gateway-Secret": GATEWAY_SECRET},
        )


def build_env(
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    vk: valkey_asyncio.Valkey,
    plans: dict[str, object],
    cfg: BillingConfig | None = None,
    make_gateway: Callable[[TestClock], FakeGateway] | None = None,
) -> Env:
    clock = TestClock(START)
    ids = Uuid7Generator()
    gateway = make_gateway(clock) if make_gateway else FakeGateway(clock=clock)
    gateway.plans.update(plans)  # type: ignore[arg-type]

    def uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(app_engine, ids, clock)

    entitlements = EntitlementService(
        uow_factory, ValkeyEntitlementCache(vk), clock, l1_ttl_seconds=0
    )
    invalidated: list[UUID] = []

    async def invalidate(user_id: UUID) -> None:
        invalidated.append(user_id)
        await entitlements.invalidate(user_id)

    bctx = BillingContext(uow_factory, gateway, clock, ids, cfg or config(), invalidate)
    applier = EventApplier(bctx)
    storage = InMemoryStorage()
    counters = ValkeyCounters(vk)
    invoices = InvoiceService(uow_factory, WeasyPrintRenderer(), storage, clock, bctx.config)
    return Env(
        app_engine=app_engine,
        migrator_engine=migrator_engine,
        clock=clock,
        gateway=gateway,
        bctx=bctx,
        applier=applier,
        service=SubscriptionService(bctx, applier),
        processor=WebhookProcessor(bctx, applier),
        runner=JobRunner(
            JobContext(
                uow_factory,
                clock,
                ids,
                gateway=gateway,
                billing=bctx,
                applier=applier,
                invoices=invoices,
                counters=counters,
            ),
            poll_interval=0.01,
        ),
        entitlements=entitlements,
        storage=storage,
        counters=counters,
        invoices=invoices,
        invalidated=invalidated,
    )
