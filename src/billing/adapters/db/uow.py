"""SqlUnitOfWork: one transaction per use case; every repo shares its connection.

Lock order (LLD "Concurrency rules"). A use case that needs several of these takes them in this
order, so two transactions can never deadlock:

    billing_accounts -> subscriptions -> ledger_balances -> invoice_counters

Inside LedgerRepo.post, accounts and balances are also touched in sorted code order.
"""

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from billing.adapters.clock import SystemClock
from billing.adapters.db.repos.accounts import SqlAccountRepo
from billing.adapters.db.repos.admin import SqlAdminRepo
from billing.adapters.db.repos.changes import SqlChangeRepo
from billing.adapters.db.repos.invoices import SqlInvoiceRepo
from billing.adapters.db.repos.ledger import SqlLedgerRepo
from billing.adapters.db.repos.overrides import SqlOverrideRepo
from billing.adapters.db.repos.payments import SqlPaymentRepo
from billing.adapters.db.repos.plans import SqlPlanRepo
from billing.adapters.db.repos.plumbing import (
    SqlAudit,
    SqlHousekeeping,
    SqlIdempotency,
    SqlJobs,
    SqlOutbox,
    SqlProcessedEvents,
    SqlWebhookInbox,
)
from billing.adapters.db.repos.pricing import SqlPricingRepo
from billing.adapters.db.repos.refunds import SqlRefundRepo
from billing.adapters.db.repos.subscriptions import SqlSubscriptionRepo
from billing.adapters.db.repos.topups import SqlTopupRepo
from billing.adapters.db.repos.usage import SqlUsageRepo
from billing.adapters.db.repos.wallets import SqlWalletRepo
from billing.adapters.kafka.codec import EventCodec
from billing.app.ports import Clock, IdGenerator


class SqlUnitOfWork:
    def __init__(self, engine: AsyncEngine, ids: IdGenerator, clock: Clock | None = None) -> None:
        self._engine = engine
        self._ids = ids
        self._clock = clock or SystemClock()
        self._conn: AsyncConnection | None = None
        self._trans: AsyncTransaction | None = None
        self._after_commit: list[Callable[[], Awaitable[None]]] = []

    def after_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._after_commit.append(callback)

    async def __aenter__(self) -> Self:
        self._after_commit = []
        conn = await self._engine.connect()
        self._conn = conn
        self._trans = await conn.begin()
        await conn.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
        await conn.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        ids = self._ids
        self.accounts = SqlAccountRepo(conn, ids)
        self.plans = SqlPlanRepo(conn)
        self.pricing = SqlPricingRepo(conn)
        self.subscriptions = SqlSubscriptionRepo(conn)
        self.overrides = SqlOverrideRepo(conn)
        self.usage = SqlUsageRepo(conn)
        self.payments = SqlPaymentRepo(conn)
        self.refunds = SqlRefundRepo(conn)
        self.changes = SqlChangeRepo(conn)
        self.invoices = SqlInvoiceRepo(conn)
        self.ledger = SqlLedgerRepo(conn, ids)
        self.wallets = SqlWalletRepo(conn, ids, self.ledger)
        self.topups = SqlTopupRepo(conn)
        self.publisher = SqlOutbox(conn, EventCodec(ids, self._clock))
        self.processed = SqlProcessedEvents(conn)
        self.idempotency = SqlIdempotency(conn)
        self.inbox = SqlWebhookInbox(conn, self._clock)
        self.jobs = SqlJobs(conn)
        self.audit = SqlAudit(conn)
        self.housekeeping = SqlHousekeeping(conn)
        self.admin = SqlAdminRepo(conn)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._conn is not None and self._trans is not None
        committed = False
        try:
            if exc_type is None:
                await self._trans.commit()
                committed = True
            else:
                await self._trans.rollback()
        finally:
            await self._conn.close()
            self._conn = None
            self._trans = None
            callbacks, self._after_commit = self._after_commit, []
        if committed:
            for callback in callbacks:
                try:
                    await callback()
                except Exception:  # the commit stands; a stale cache entry expires on its own
                    structlog.get_logger().exception("after_commit_failed")

    @property
    def connection(self) -> AsyncConnection:
        """Escape hatch for raw SQL in tests and one-off maintenance."""
        assert self._conn is not None
        return self._conn
