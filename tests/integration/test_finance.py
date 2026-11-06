"""Settlements (bank, fees, refunds) and monthly revenue recognition."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from billing.app.settlements import (
    RevenueService,
    SettlementService,
    month_index,
    slice_for,
    split_deductions,
)
from billing.domain.catalog import Interval
from billing.domain.money import Paise, taxable_of
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env

PRO_YEAR = 499000
DAY = date(2026, 11, 2)


async def balance(env: Env, code: str) -> int:
    value = await env.scalar(
        "SELECT b.balance_paise FROM billing.ledger_balances b "
        "JOIN billing.ledger_accounts a ON a.id = b.account_id WHERE a.code = :c",
        c=code,
    )
    return int(value or 0)


async def unbalanced(env: Env) -> list[object]:
    return list(
        await env.sql(
            "SELECT transaction_id FROM billing.ledger_entries GROUP BY 1 "
            "HAVING sum(amount_paise) <> 0"
        )
    )


async def charged_payment(env: Env) -> str:
    return str(await env.scalar("SELECT gateway_payment_id FROM billing.payments LIMIT 1"))


def test_split_deductions_works_whether_or_not_the_fee_includes_gst() -> None:
    # Razorpay held back 1,178 of 49,900, of which 180 is GST on the fee
    assert split_deductions(Paise(49900), Paise(48722), Paise(180)) == (998, 180)
    assert split_deductions(Paise(49900), Paise(49900), Paise(0)) == (0, 0)  # nothing held
    assert split_deductions(Paise(100), Paise(100), Paise(50)) == (0, 0)  # tax capped at the hold
    assert split_deductions(Paise(100), Paise(150), Paise(0)) == (0, 0)  # never negative


def test_the_twelve_slices_add_up_to_the_taxable_amount() -> None:
    for taxable in (422_881, 1_270_338, 12, 11, 1):
        slices = [slice_for(Paise(taxable), n) for n in range(1, 13)]
        assert sum(slices) == taxable and min(slices) >= 0
        assert slices[:-1] == [taxable // 12] * 11
    assert month_index(datetime(2026, 11, 30, 20, 30, tzinfo=UTC)) == month_index(
        datetime(2026, 12, 1, 2, 0, tzinfo=UTC)
    )  # 02:00 IST on 1 Dec is still 30 Nov in UTC


# --- settlements -----------------------------------------------------------------------------


async def test_a_settled_payment_posts_bank_fee_and_input_gst(env: Env) -> None:
    await env.activate()
    pay = await charged_payment(env)
    env.gateway.settle_payment(pay, DAY, fee=1178, tax=180)
    await env.run_job("settlements.sync", {"day": DAY.isoformat()})

    assert await balance(env, "asset:bank") == 48722
    assert await balance(env, "expense:gateway_fees") == 998
    assert await balance(env, "asset:gst_input") == 180
    assert await balance(env, "asset:razorpay_clearing") == 0  # the charge's 49,900 is settled
    assert await unbalanced(env) == []

    await env.run_job("settlements.sync", {"day": DAY.isoformat()})  # again: nothing changes
    assert await balance(env, "asset:bank") == 48722
    key = "SELECT count(*) FROM billing.ledger_transactions WHERE kind = 'settlement'"
    assert await env.scalar(key) == 1


async def test_an_unsettled_line_and_a_stranger_are_left_alone(env: Env) -> None:
    await env.activate()
    pay = await charged_payment(env)
    env.gateway.settle_payment(pay, DAY, fee=1178, tax=180, settled=False)
    ours = env.gateway.settlements[DAY][0]
    env.gateway.settlements[DAY].append(replace(ours, entity_id="pay_not_ours", settled=True))
    report = await SettlementService(env.bctx.uow_factory, env.gateway, env.clock).sync(DAY)
    assert (report.payments_posted, report.unsettled, report.unknown) == (0, 1, 1)
    assert await balance(env, "asset:bank") == 0


async def test_a_refund_taken_out_of_a_settlement_posts_clearing_to_bank(env: Env) -> None:
    await env.activate()
    env.clock.set(datetime(2026, 11, 16, 4, 45, tzinfo=UTC))
    up = await env.service.change_plan(env.user, "max", Interval.MONTH)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    await env.run_jobs()  # the refund of 24,950 and its credit note
    refund = next(iter(env.gateway.refunds.values()))
    pays = [p for p in env.gateway.payments.values()]
    for p in pays:
        env.gateway.settle_payment(p.id, DAY, fee=0, tax=0)
    env.gateway.settle_refund(refund.id, DAY)
    report = await SettlementService(env.bctx.uow_factory, env.gateway, env.clock).sync(DAY)
    assert report.refunds_posted == 1 and report.payments_posted == 2
    # banked: both charges (49,900 + 149,900) less the refund (24,950)
    assert await balance(env, "asset:bank") == PRO_MONTH + MAX_MONTH - 24950
    assert await balance(env, "asset:razorpay_clearing") == 0
    assert await unbalanced(env) == []
    again = await SettlementService(env.bctx.uow_factory, env.gateway, env.clock).sync(DAY)
    assert again.already_posted == 3 and again.refunds_posted == 0


async def test_the_manual_fallback_posts_once_and_the_sync_then_skips_it(env: Env) -> None:
    await env.activate()
    pay = await charged_payment(env)
    payment_id = await env.scalar("SELECT id FROM billing.payments")
    service = SettlementService(env.bctx.uow_factory, env.gateway, env.clock)
    assert await service.post_manual(payment_id, Paise(998), Paise(180)) is True  # type: ignore[arg-type]
    assert await service.post_manual(payment_id, Paise(998), Paise(180)) is False  # type: ignore[arg-type]
    assert await balance(env, "asset:bank") == 48722
    env.gateway.settle_payment(pay, DAY, fee=1178, tax=180)
    report = await service.sync(DAY)
    assert report.already_posted == 1 and await balance(env, "asset:bank") == 48722
    with pytest.raises(LookupError):
        await service.post_manual(uuid4(), Paise(1), Paise(0))


# --- revenue recognition ---------------------------------------------------------------------


def first_of_month_0200_ist(year: int, month: int) -> datetime:
    return datetime(year, month, 1, 2, 0, tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def test_a_yearly_plan_is_recognised_over_twelve_months_to_the_paisa(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    taxable = taxable_of(Paise(PRO_YEAR))
    assert await balance(env, "liability:deferred_revenue") == -taxable  # all deferred at first
    assert await balance(env, "revenue:subscription") == 0

    revenue = RevenueService(env.bctx.uow_factory, env.clock)
    runs = [first_of_month_0200_ist(2026, 12)]
    runs += [first_of_month_0200_ist(2027, m) for m in range(1, 12)]  # Jan .. Nov 2027
    assert len(runs) == 12
    for n, run in enumerate(runs, start=1):
        env.clock.set(run)
        assert await revenue.recognise() == 1
        assert await revenue.recognise() == 0  # the same month again posts nothing
        assert await balance(env, "revenue:subscription") == -sum(
            slice_for(taxable, k) for k in range(1, n + 1)
        )
        assert await unbalanced(env) == []

    assert await balance(env, "liability:deferred_revenue") == 0  # fully recognised
    assert await balance(env, "revenue:subscription") == -taxable
    env.clock.set(first_of_month_0200_ist(2027, 12))  # a 13th run: nothing left
    assert await revenue.recognise() == 0


async def test_monthly_plans_and_the_month_of_purchase_are_not_recognised(env: Env) -> None:
    await env.activate()  # monthly: revenue was booked at the charge
    revenue = RevenueService(env.bctx.uow_factory, env.clock)
    env.clock.set(first_of_month_0200_ist(2026, 12))
    assert await revenue.recognise() == 0
    assert await balance(env, "revenue:subscription") == -taxable_of(Paise(PRO_MONTH))


async def test_the_recognition_job_runs_on_the_first(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    env.clock.set(first_of_month_0200_ist(2026, 12))
    await env.run_job("revenue.recognise")
    await env.run_job("revenue.recognise")
    assert await balance(env, "revenue:subscription") == -slice_for(taxable_of(Paise(PRO_YEAR)), 1)
