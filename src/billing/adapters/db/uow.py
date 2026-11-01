"""SqlUnitOfWork: one transaction per use case; every repo shares its connection.

Lock order (LLD "Concurrency rules"). A use case that needs several of these takes them in this
order, so two transactions can never deadlock:

    billing_accounts -> subscriptions -> ledger_balances -> invoice_counters

Inside LedgerRepo.post, accounts and balances are also touched in sorted code order.
"""

from types import TracebackType
from typing import Self

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from billing.adapters.db.repos.accounts import SqlAccountRepo
from billing.adapters.db.repos.invoices import SqlInvoiceRepo
from billing.adapters.db.repos.ledger import SqlLedgerRepo
from billing.adapters.db.repos.overrides import SqlOverrideRepo
from billing.adapters.db.repos.payments import SqlPaymentRepo
from billing.adapters.db.repos.plans import SqlPlanRepo
from billing.adapters.db.repos.plumbing import (
    SqlAudit,
    SqlIdempotency,
    SqlJobs,
    SqlOutbox,
    SqlProcessedEvents,
    SqlWebhookInbox,
)
from billing.adapters.db.repos.pricing import SqlPricingRepo
from billing.adapters.db.repos.subscriptions import SqlSubscriptionRepo
from billing.adapters.db.repos.usage import SqlUsageRepo
from billing.app.ports import IdGenerator


class SqlUnitOfWork:
    def __init__(self, engine: AsyncEngine, ids: IdGenerator) -> None:
        self._engine = engine
        self._ids = ids
        self._conn: AsyncConnection | None = None
        self._trans: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
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
        self.invoices = SqlInvoiceRepo(conn)
        self.ledger = SqlLedgerRepo(conn, ids)
        self.publisher = SqlOutbox(conn)
        self.processed = SqlProcessedEvents(conn)
        self.idempotency = SqlIdempotency(conn)
        self.inbox = SqlWebhookInbox(conn)
        self.jobs = SqlJobs(conn)
        self.audit = SqlAudit(conn)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._conn is not None and self._trans is not None
        try:
            if exc_type is None:
                await self._trans.commit()
            else:
                await self._trans.rollback()
        finally:
            await self._conn.close()
            self._conn = None
            self._trans = None

    @property
    def connection(self) -> AsyncConnection:
        """Escape hatch for raw SQL in tests and one-off maintenance."""
        assert self._conn is not None
        return self._conn
