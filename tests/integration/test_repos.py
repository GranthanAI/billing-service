import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.db.engine import make_engine
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.app.records import (
    BillingAccount,
    InProgress,
    Mismatch,
    New,
    Replay,
    StaleRecord,
    SubscriptionRecord,
)
from billing.domain import ledger
from billing.domain.catalog import Interval
from billing.domain.money import Paise, split_gst
from billing.domain.subscription import SubscriptionState, SubStatus
from tests.integration.conftest import Urls
from tests.support import plans

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
Make = Callable[[], SqlUnitOfWork]


def build(kind: str, wallet: UUID, amount: int, key: str) -> ledger.LedgerTransaction:
    split = split_gst(Paise(amount), "10", "10")
    if kind == "topup":
        return ledger.wallet_topup(split, wallet, key)
    if kind == "usage":
        return ledger.payg_usage(Paise(amount), wallet, key)
    if kind == "expiry":
        return ledger.credit_expiry(Paise(amount), wallet, key)
    return ledger.subscription_charge(split, Interval.MONTH, key)


async def test_invoice_numbers_are_gapless_under_concurrency(make_uow: Make) -> None:
    series = f"T{uuid4().hex[:8]}"

    async def allocate() -> str:
        async with make_uow() as uow:
            return await uow.invoices.allocate_number(series, "2627")

    numbers = await asyncio.gather(*(allocate() for _ in range(50)))
    assert sorted(numbers) == [f"{series}/2627/{n:06d}" for n in range(1, 51)]


async def test_rolled_back_number_is_reused_and_year_restarts(make_uow: Make) -> None:
    series = f"T{uuid4().hex[:8]}"
    async with make_uow() as uow:
        assert await uow.invoices.allocate_number(series, "2627") == f"{series}/2627/000001"
    with pytest.raises(RuntimeError):
        async with make_uow() as uow:
            assert await uow.invoices.allocate_number(series, "2627") == f"{series}/2627/000002"
            raise RuntimeError("boom")
    async with make_uow() as uow:
        assert await uow.invoices.allocate_number(series, "2627") == f"{series}/2627/000002"
        assert await uow.invoices.allocate_number(series, "2728") == f"{series}/2728/000001"


async def test_ledger_post_is_idempotent(make_uow: Make) -> None:
    wallet = uuid4()
    key = f"k-{uuid4()}"
    tx = ledger.credit_expiry(Paise(700), wallet, key)
    async with make_uow() as uow:
        assert await uow.ledger.post(tx, "test", "1", NOW)
        assert not await uow.ledger.post(tx, "test", "1", NOW)
        assert await uow.ledger.balance(ledger.wallet_code(wallet)) == 700
        assert await uow.ledger.balance(ledger.REVENUE_BREAKAGE) <= -700
        assert await uow.ledger.lock_balance(ledger.wallet_code(wallet)) == 700
        assert await uow.ledger.balance("liability:wallet:never-used") == 0


def test_ledger_balances_equal_sum_of_entries(urls: Urls, migrated: Urls) -> None:
    amounts = st.integers(min_value=1000, max_value=10_000_000)

    @settings(max_examples=100, deadline=None)
    @given(
        st.lists(
            st.tuples(st.sampled_from(["topup", "usage", "expiry", "charge"]), amounts),
            min_size=1,
            max_size=8,
        )
    )
    def check(steps: list[tuple[str, int]]) -> None:
        asyncio.run(run(steps))

    async def run(steps: list[tuple[str, int]]) -> None:
        engine = make_engine(migrated.app, pool_size=2)
        wallet = uuid4()
        try:
            async with SqlUnitOfWork(engine, Uuid7Generator()) as uow:
                for kind, amount in steps:
                    key = f"p-{uuid4()}"
                    tx = build(kind, wallet, amount, key)
                    await uow.ledger.post(tx, "test", key, NOW)
                rows = await uow.connection.execute(
                    sa.text(
                        "SELECT a.code, b.balance_paise, "
                        "(SELECT sum(e.amount_paise) FROM billing.ledger_entries e "
                        " WHERE e.account_id = a.id) AS total "
                        "FROM billing.ledger_balances b JOIN billing.ledger_accounts a "
                        "ON a.id = b.account_id WHERE a.code LIKE '%' || :w || '%' "
                        "OR a.code LIKE 'liability:gst_output:%' OR a.code LIKE 'revenue:%' "
                        "OR a.code = 'asset:razorpay_clearing'"
                    ),
                    {"w": str(wallet)},
                )
                for r in rows:
                    assert r.balance_paise == r.total, r.code
        finally:
            await engine.dispose()

    check()


async def test_outbox_rolls_back_with_the_unit_of_work(make_uow: Make) -> None:
    key = f"k-{uuid4()}"
    with pytest.raises(RuntimeError):
        async with make_uow() as uow:
            await uow.publisher.enqueue("t", key, "x.happened", b"\x01", {"h": "v"})
            raise RuntimeError("boom")
    async with make_uow() as uow:
        count = (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.outbox WHERE key = :k"), {"k": key}
            )
        ).scalar_one()
        assert count == 0
        await uow.publisher.enqueue("t", key, "x.happened", b"\x01", {"h": "v"})
    async with make_uow() as uow:
        count = (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.outbox WHERE key = :k"), {"k": key}
            )
        ).scalar_one()
        assert count == 1


async def test_processed_claim_and_idempotency(make_uow: Make) -> None:
    ev = f"e-{uuid4()}"
    async with make_uow() as uow:
        assert await uow.processed.claim("billing-usage", ev)
        assert not await uow.processed.claim("billing-usage", ev)
        assert await uow.processed.claim("billing-org", ev)  # per consumer

    scope, key = f"u-{uuid4()}", "idem-1"
    async with make_uow() as uow:
        assert isinstance(await uow.idempotency.begin(scope, key, "h1", 60), New)
    async with make_uow() as uow:
        assert isinstance(await uow.idempotency.begin(scope, key, "h1", 60), InProgress)
        assert isinstance(await uow.idempotency.begin(scope, key, "other", 60), Mismatch)
        await uow.idempotency.complete(scope, key, 201, {"ok": True})
    async with make_uow() as uow:
        assert await uow.idempotency.begin(scope, key, "h1", 60) == Replay(201, {"ok": True})
        assert isinstance(await uow.idempotency.begin(scope, key, "other", 60), Mismatch)

    # a crashed first attempt: once its lock expires another request may take over
    key2 = "idem-2"
    async with make_uow() as uow:
        assert isinstance(await uow.idempotency.begin(scope, key2, "h", 0), New)
    await asyncio.sleep(0.05)
    async with make_uow() as uow:
        assert isinstance(await uow.idempotency.begin(scope, key2, "h", 60), New)


async def test_inbox_and_jobs_dedupe(make_uow: Make) -> None:
    ev = f"e-{uuid4()}"
    dedupe = f"d-{uuid4()}"
    async with make_uow() as uow:
        assert await uow.inbox.store("razorpay", ev, "payment.captured", {"a": 1})
        assert not await uow.inbox.store("razorpay", ev, "payment.captured", {"a": 1})
        assert await uow.jobs.enqueue("reconcile", {}, NOW, dedupe)
        assert not await uow.jobs.enqueue("reconcile", {}, NOW, dedupe)
        assert await uow.jobs.enqueue("x", {}, NOW, None)
        assert await uow.jobs.enqueue("x", {}, NOW, None)
        await uow.audit.record(uuid4(), "grant", "override", "1", "goodwill", None, {"a": 1})


async def test_subscription_cas_and_lookup(
    make_uow: Make, account: BillingAccount, seeded_plans: None
) -> None:
    gw = f"sub_{uuid4().hex[:10]}"
    rec = SubscriptionRecord(
        id=uuid4(),
        billing_account_id=account.id,
        state=SubscriptionState(
            SubStatus.ACTIVE,
            plans.PRO.id,
            Interval.MONTH,
            gateway_subscription_id=gw,
            next_gateway_subscription_id=f"{gw}_next",
            last_period_paid=Paise(49900),
        ),
    )
    async with make_uow() as uow:
        await uow.subscriptions.insert(rec)
    async with make_uow() as uow:
        stale = await uow.subscriptions.get(rec.id)
        assert stale == rec
        saved = await uow.subscriptions.save(
            replace(stale, state=replace(stale.state, status=SubStatus.PAST_DUE))
        )
        assert saved.lock_version == 1
    with pytest.raises(StaleRecord):
        async with make_uow() as uow:
            await uow.subscriptions.save(stale)
    async with make_uow() as uow:
        by_next = await uow.subscriptions.lock_by_gateway_id(f"{gw}_next")
        assert by_next is not None and by_next.id == rec.id
        live = await uow.subscriptions.live_for_account(account.id)
        assert live is not None and live.state.status is SubStatus.PAST_DUE


async def test_lock_order_does_not_deadlock(
    make_uow: Make, account: BillingAccount, seeded_plans: None
) -> None:
    """Every task takes billing_accounts -> subscriptions -> ledger_balances -> invoice_counters."""
    wallet = account.id
    series = f"L{uuid4().hex[:8]}"
    sub = SubscriptionRecord(
        id=uuid4(),
        billing_account_id=account.id,
        state=SubscriptionState(SubStatus.ACTIVE, plans.PRO.id, Interval.MONTH),
    )
    async with make_uow() as uow:
        await uow.subscriptions.insert(sub)
        await uow.ledger.post(
            ledger.credit_expiry(Paise(100), wallet, f"seed-{uuid4()}"), "test", "1", NOW
        )

    async def task() -> None:
        async with make_uow() as uow:
            await uow.connection.execute(
                sa.text("SELECT id FROM billing.billing_accounts WHERE id = :i FOR UPDATE"),
                {"i": account.id},
            )
            await uow.subscriptions.lock(sub.id)
            await uow.ledger.lock_balance(ledger.wallet_code(wallet))
            await uow.invoices.allocate_number(series, "2627")
            await asyncio.sleep(0.01)

    await asyncio.gather(*(task() for _ in range(10)))


async def test_plan_and_override_roundtrip(
    make_migrator_uow: Make, make_uow: Make, seeded_plans: None, account: BillingAccount
) -> None:
    from billing.app.records import OverrideRecord
    from billing.domain.catalog import OwnerType

    async with make_uow() as uow:
        rec = await uow.plans.by_id(plans.MAX.id)
        assert rec is not None and rec.plan == plans.MAX
        current = await uow.plans.current("max", None)
        assert current is not None and current.plan.id == plans.MAX.id
        assert {p.plan.plan_code for p in await uow.plans.all_current()} >= {"free", "pro", "max"}
        ov = OverrideRecord(
            id=uuid4(),
            owner_type=OwnerType.USER,
            owner_id=account.owner_id,
            plan_version_id=plans.MAX.id,
            reason="demo",
            starts_at=NOW,
            expires_at=NOW.replace(year=2027),
            granted_by=uuid4(),
        )
        await uow.overrides.insert(ov)
        assert [
            o.id for o in await uow.overrides.active_for(OwnerType.USER, account.owner_id, NOW)
        ] == [ov.id]
        await uow.overrides.end(ov.id, NOW)
        assert await uow.overrides.active_for(OwnerType.USER, account.owner_id, NOW) == []
        await uow.accounts.set_state_code(account.id, "10")
        await uow.accounts.set_gstin(account.id, "10ABCDE1234F1Z5", "Acme")
        got = await uow.accounts.get_by_owner(OwnerType.USER, account.owner_id)
        assert got is not None and got.state_code == "10" and got.legal_name == "Acme"
    async with make_migrator_uow() as uow:  # catalog sync runs as the migrator
        await uow.plans.set_gateway_plan_ids(plans.PRO.id, "plan_m", "plan_y")
        await uow.plans.retire(plans.PRO.id, NOW)
        await uow.plans.set_gateway_plan_ids(plans.PRO.id, None, None)
    async with make_migrator_uow() as uow:
        await uow.connection.execute(
            sa.text("UPDATE billing.plan_versions SET retired_at = NULL WHERE id = :i"),
            {"i": plans.PRO.id},
        )


async def test_app_role_cannot_write_plan_versions(
    app_engine: AsyncEngine, seeded_plans: None
) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(sa.text("UPDATE billing.plan_versions SET rank = rank"))
