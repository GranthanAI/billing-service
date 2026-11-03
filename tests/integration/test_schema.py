import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from billing.adapters.db.partitions import ensure
from billing.adapters.db.tables import metadata
from billing.adapters.db.uow import SqlUnitOfWork
from billing.app.records import BillingAccount, UsageEventRecord
from billing.domain import ledger
from billing.domain.catalog import ModelGroup, OwnerType
from billing.domain.money import MicroInr, Paise
from tests.integration.conftest import Urls, alembic_config
from tests.support import plans


async def _create_db(urls: Urls, name: str) -> None:
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
    finally:
        await conn.close()


async def _version(url: str) -> str | None:
    from billing.adapters.db.engine import make_engine

    engine = make_engine(url)
    try:
        async with engine.connect() as c:
            return (
                await c.execute(sa.text("SELECT version_num FROM billing.alembic_version"))
            ).scalar_one_or_none()
    finally:
        await engine.dispose()


def test_upgrade_downgrade_upgrade(urls: Urls) -> None:
    name = f"mig_{uuid4().hex[:8]}"
    asyncio.run(_create_db(urls, name))
    url = urls.migrator_for(name)
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    assert asyncio.run(_version(url)) == "0012_catalog_models"
    command.downgrade(cfg, "base")
    assert asyncio.run(_version(url)) is None
    command.upgrade(cfg, "head")
    assert asyncio.run(_version(url)) == "0012_catalog_models"


async def test_metadata_matches_migrations(migrated: Urls) -> None:
    def include_object(
        obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
    ) -> bool:
        if type_ == "table" and name and name.startswith("usage_events_20"):
            return False  # partitions
        if type_ == "table" and name == "alembic_version":
            return False
        return not (type_ == "index" and name and "usage_events_20" in name)

    def run(sync_conn: sa.Connection) -> list[object]:
        ctx = MigrationContext.configure(
            sync_conn,
            opts={
                "include_schemas": True,
                "include_object": include_object,
                "include_name": lambda name, type_, parent: (
                    name == "billing" if type_ == "schema" else True
                ),
            },
        )
        return list(compare_metadata(ctx, metadata))

    # A plain engine: its default schema is public, so alembic keeps 'billing' distinct from it
    # (the app engines set search_path = billing, which alembic would fold into the default).
    engine = create_async_engine(migrated.migrator)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(run)
    finally:
        await engine.dispose()
    assert diffs == []


async def test_seeded_ledger_accounts_cover_domain_codes(
    make_uow: Callable[[], SqlUnitOfWork],
) -> None:
    codes = {v for k, v in vars(ledger).items() if k.isupper() and isinstance(v, str) and ":" in v}
    assert len(codes) == 11
    async with make_uow() as uow:
        rows = await uow.connection.execute(sa.text("SELECT code FROM billing.ledger_accounts"))
        seeded = {r.code for r in rows}
    assert codes <= seeded


def _usage(account: BillingAccount, at: datetime, source: str) -> UsageEventRecord:
    return UsageEventRecord(
        kind="message",
        source_id=source,
        occurred_at=at,
        user_id=account.owner_id,
        billing_account_id=account.id,
        plan_version_id=plans.FREE.id,
        billable_units=1,
        cost_micro_inr=MicroInr(100),
        fx_usd_inr="95.0000",
        model_group=ModelGroup.FAST,
        price_paise=Paise(0),
    )


async def test_app_role_cannot_change_append_only_tables(
    make_uow: Callable[[], SqlUnitOfWork],
    make_migrator_uow: Callable[[], SqlUnitOfWork],
    account: BillingAccount,
) -> None:
    now = datetime.now(UTC)
    wallet = uuid4()
    async with make_uow() as uow:
        tx = ledger.credit_expiry(Paise(500), wallet, f"k-{uuid4()}")
        assert await uow.ledger.post(tx, "test", "1", now)
        assert await uow.usage.insert(_usage(account, now, f"src-{uuid4()}"))

    statements = [
        "UPDATE billing.ledger_entries SET amount_paise = amount_paise",
        "DELETE FROM billing.ledger_entries",
        "UPDATE billing.usage_events SET cost_micro_inr = cost_micro_inr",
        "DELETE FROM billing.usage_events",
    ]
    for stmt in statements:
        with pytest.raises(DBAPIError, match="permission denied"):
            async with make_uow() as uow:
                await uow.connection.execute(sa.text(stmt))
        # the owner has the privilege, but the trigger refuses
        with pytest.raises(DBAPIError, match="append-only"):
            async with make_migrator_uow() as uow:
                await uow.connection.execute(sa.text(stmt))


async def test_unbalanced_ledger_fails_at_commit(
    make_uow: Callable[[], SqlUnitOfWork],
) -> None:
    async def insert(amounts: list[int]) -> None:
        async with make_uow() as uow:
            c = uow.connection
            acct = (
                await c.execute(
                    sa.text("SELECT id FROM billing.ledger_accounts WHERE code = 'asset:bank'")
                )
            ).scalar_one()
            tx_id = uuid4()
            await c.execute(
                sa.text(
                    "INSERT INTO billing.ledger_transactions "
                    "(id, kind, ref_type, ref_id, idempotency_key, occurred_at) "
                    "VALUES (:id, 'settlement', 't', '1', :k, now())"
                ),
                {"id": tx_id, "k": f"raw-{tx_id}"},
            )
            for amount in amounts:
                await c.execute(
                    sa.text(
                        "INSERT INTO billing.ledger_entries (transaction_id, account_id, "
                        "amount_paise) VALUES (:t, :a, :n)"
                    ),
                    {"t": tx_id, "a": acct, "n": amount},
                )

    with pytest.raises(DBAPIError, match="does not balance"):
        await insert([100, -99])
    await insert([100, -100])


async def test_one_live_subscription_per_account(
    make_uow: Callable[[], SqlUnitOfWork], seeded_plans: None
) -> None:
    from billing.app.records import SubscriptionRecord
    from billing.domain.catalog import Interval
    from billing.domain.subscription import SubscriptionState, SubStatus

    async with make_uow() as uow:
        acct = await uow.accounts.get_or_create(OwnerType.USER, uuid4(), "a@example.com")

    def sub(status: SubStatus) -> SubscriptionRecord:
        return SubscriptionRecord(
            id=uuid4(),
            billing_account_id=acct.id,
            state=SubscriptionState(status, plans.PRO.id, Interval.MONTH),
        )

    async with make_uow() as uow:
        await uow.subscriptions.insert(sub(SubStatus.ACTIVE))
        await uow.subscriptions.insert(sub(SubStatus.INCOMPLETE))  # not live: allowed
        await uow.subscriptions.insert(sub(SubStatus.INCOMPLETE))
    with pytest.raises(DBAPIError, match="subscriptions_one_live"):
        async with make_uow() as uow:
            await uow.subscriptions.insert(sub(SubStatus.PAST_DUE))


async def test_partitions(
    app_engine: AsyncEngine,
    make_uow: Callable[[], SqlUnitOfWork],
    migrator_engine: AsyncEngine,
    account: BillingAccount,
) -> None:
    async with app_engine.begin() as conn:
        assert await ensure(conn, 2) == 0  # the migration already created them
    async with app_engine.begin() as conn:
        assert await ensure(conn, 2) == 0  # idempotent

    async with migrator_engine.begin() as conn:
        for part, lo, hi in (
            ("usage_events_2026_11", "2026-11-01 00:00+05:30", "2026-12-01 00:00+05:30"),
            ("usage_events_2026_12", "2026-12-01 00:00+05:30", "2027-01-01 00:00+05:30"),
        ):
            await conn.execute(
                sa.text(
                    f"CREATE TABLE IF NOT EXISTS billing.{part} PARTITION OF "
                    f"billing.usage_events FOR VALUES FROM ('{lo}') TO ('{hi}')"
                )
            )

    edge = datetime(2026, 11, 30, 18, 30, tzinfo=UTC)  # 00:00 IST on 1 Dec
    before = datetime(2026, 11, 30, 18, 29, 59, tzinfo=UTC)
    src = f"src-{uuid4()}"
    async with make_uow() as uow:
        assert await uow.usage.insert(_usage(account, edge, src))
        assert await uow.usage.insert(_usage(account, before, src))
        rows = await uow.connection.execute(
            sa.text(
                "SELECT tableoid::regclass::text AS part, occurred_at FROM billing.usage_events "
                "WHERE source_id = :s ORDER BY occurred_at"
            ),
            {"s": src},
        )
        parts = [r.part for r in rows]
    assert parts == ["usage_events_2026_11", "usage_events_2026_12"]


async def test_usage_insert_conflict_and_rollup(
    make_uow: Callable[[], SqlUnitOfWork], account: BillingAccount
) -> None:
    from datetime import date

    at = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    async with make_uow() as uow:
        await ensure(uow.connection, 2)
    src = f"src-{uuid4()}"
    async with make_uow() as uow:
        assert await uow.usage.insert(_usage(account, at, src))
        assert not await uow.usage.insert(_usage(account, at, src))
        assert await uow.usage.counts_for_day(account.owner_id, date(2026, 11, 10)) == {
            ModelGroup.FAST: 1
        }
        assert await uow.usage.counts_for_day(account.owner_id, date(2026, 11, 11)) == {}
        assert await uow.usage.rollup_day(date(2026, 11, 10)) >= 1
        assert await uow.usage.rollup_day(date(2026, 11, 10)) >= 1  # idempotent
        row = (
            await uow.connection.execute(
                sa.text(
                    "SELECT messages FROM billing.usage_daily "
                    "WHERE user_id = :u AND day = '2026-11-10'"
                ),
                {"u": account.owner_id},
            )
        ).scalar_one()
        assert row == 1
