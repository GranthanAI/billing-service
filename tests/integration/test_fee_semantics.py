"""Razorpay's fee includes GST: fee_paise stores the fee proper, fee_gst_paise the GST in it."""

import asyncio
from datetime import date
from uuid import uuid4

import asyncpg
import structlog
from alembic import command

from billing.app.charges import fee_without_gst
from billing.app.settlements import SettlementService
from billing.domain.money import Paise
from tests.integration.billing_env import PRO_MONTH, Env
from tests.integration.conftest import Urls, alembic_config

DAY = date(2026, 11, 2)


def test_fee_without_gst() -> None:
    assert fee_without_gst(Paise(1178), Paise(180)) == 998
    assert fee_without_gst(Paise(1178), None) == 1178  # no GST reported: the fee as given
    assert fee_without_gst(None, Paise(180)) is None
    assert fee_without_gst(Paise(100), Paise(180)) == 0  # never negative
    assert fee_without_gst(Paise(0), Paise(0)) == 0


async def test_a_recorded_charge_stores_the_fee_without_its_gst(env: Env) -> None:
    await env.activate()
    row = (await env.sql("SELECT amount_paise, fee_paise, fee_gst_paise FROM billing.payments"))[0]
    amount, fee, gst = (int(x) for x in row)
    assert amount == PRO_MONTH
    pay = next(iter(env.gateway.payments.values()))
    assert pay.fee is not None and pay.tax is not None and pay.tax > 0
    assert (fee, gst) == (pay.fee - pay.tax, pay.tax)  # fee + GST is what Razorpay deducts
    assert fee + gst == pay.fee


# --- the settlement job checks the stored split against the bank credit ----------------------


async def _settle(env: Env, fee: int, tax: int) -> list[dict[str, object]]:
    pay = str(await env.scalar("SELECT gateway_payment_id FROM billing.payments"))
    env.gateway.settle_payment(pay, DAY, fee=fee, tax=tax)
    with structlog.testing.capture_logs() as logs:
        await SettlementService(env.bctx.uow_factory, env.gateway, env.clock).sync(DAY)
    return [e for e in logs if e["event"] == "settlement_fee_disagrees"]


async def test_no_warning_when_the_bank_credit_matches_the_stored_fee_and_gst(env: Env) -> None:
    await env.activate()
    stored = (await env.sql("SELECT fee_paise + fee_gst_paise FROM billing.payments"))[0][0]
    tax = int(await env.scalar("SELECT fee_gst_paise FROM billing.payments"))  # type: ignore[arg-type]
    assert await _settle(env, int(stored), tax) == []


async def test_one_paisa_of_difference_is_tolerated(env: Env) -> None:
    await env.activate()
    stored = int((await env.sql("SELECT fee_paise + fee_gst_paise FROM billing.payments"))[0][0])
    assert await _settle(env, stored + 1, 152) == []


async def test_a_bank_credit_that_disagrees_is_logged_but_still_posted(env: Env) -> None:
    await env.activate()
    stored = int((await env.sql("SELECT fee_paise + fee_gst_paise FROM billing.payments"))[0][0])
    warnings = await _settle(env, stored + 500, 152)  # Razorpay held back 5 rupees more
    assert len(warnings) == 1
    w = warnings[0]
    assert (
        w["bank_credit_deduction"] == stored + 500
        and w["stored_fee"] + w["stored_fee_gst"] == stored
    )  # type: ignore[operator]
    assert w["log_level"] == "warning"
    bank = await env.scalar(
        "SELECT b.balance_paise FROM billing.ledger_balances b JOIN billing.ledger_accounts a "
        "ON a.id = b.account_id WHERE a.code = 'asset:bank'"
    )
    assert bank == PRO_MONTH - stored - 500  # the bank credit is the truth that gets posted


async def test_a_payment_without_a_stored_fee_does_not_warn(env: Env) -> None:
    await env.activate()
    async with env.migrator_engine.begin() as conn:
        import sqlalchemy as sa

        await conn.execute(sa.text("UPDATE billing.payments SET fee_paise = NULL"))
    assert await _settle(env, 5000, 700) == []


# --- migration 0015a fixes rows written before it ---------------------------------------------


async def test_the_migration_subtracts_the_gst_from_old_fee_rows(urls: Urls) -> None:
    name = f"fee_{uuid4().hex[:8]}"
    conn = await asyncpg.connect(
        host=urls.host, port=urls.port, user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(f'CREATE DATABASE "{name}" OWNER billing_migrator')
    finally:
        await conn.close()
    cfg = alembic_config(urls.migrator_for(name))
    await asyncio.to_thread(command.upgrade, cfg, "0014_ops_tables")

    mig = await asyncpg.connect(
        host=urls.host, port=urls.port, user="billing_migrator", password="billing_migrator",
        database=name,
    )  # fmt: skip
    try:
        account = uuid4()
        await mig.execute(
            "INSERT INTO billing.billing_accounts (id, owner_type, owner_id) "
            "VALUES ($1, 'user', $2)", account, uuid4(),
        )  # fmt: skip
        rows = [
            ("pay_old_both", 49900, 1178, 180),  # fee included the GST: fixed
            ("pay_old_nofee", 49900, None, 180),  # nothing to fix
            ("pay_old_notax", 49900, 1178, None),  # nothing to fix
            ("pay_old_odd", 49900, 100, 180),  # fee smaller than the GST: left alone
        ]
        for gid, amount, fee, tax in rows:
            await mig.execute(
                "INSERT INTO billing.payments (id, billing_account_id, purpose, "
                "gateway_payment_id, amount_paise, fee_paise, fee_gst_paise, status, captured_at, "
                "raw) VALUES ($1, $2, 'subscription', $3, $4, $5, $6, 'captured', now(), '{}')",
                uuid4(), account, gid, amount, fee, tax,
            )  # fmt: skip
        await asyncio.to_thread(command.upgrade, cfg, "head")
        got = {
            r["gateway_payment_id"]: (r["fee_paise"], r["fee_gst_paise"])
            for r in await mig.fetch(
                "SELECT gateway_payment_id, fee_paise, fee_gst_paise FROM billing.payments"
            )
        }
        assert got == {
            "pay_old_both": (998, 180),
            "pay_old_nofee": (None, 180),
            "pay_old_notax": (1178, None),
            "pay_old_odd": (100, 180),
        }
        await asyncio.to_thread(command.downgrade, cfg, "0014_ops_tables")
        back = await mig.fetchrow(
            "SELECT fee_paise FROM billing.payments WHERE gateway_payment_id = 'pay_old_both'"
        )
        assert back is not None and back["fee_paise"] == 1178
    finally:
        await mig.close()
