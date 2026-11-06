"""Deferred revenue returns to zero: catch-up slices, and release on an early end."""

from datetime import UTC, datetime, timedelta

from billing.app.charges import issue_refund
from billing.app.revenue import RevenueService, deferred_remaining, release_deferred, slice_for
from billing.app.user_deletion import UserDeletionService
from billing.domain.catalog import Interval
from billing.domain.money import Paise, taxable_of
from tests.integration.billing_env import Env

PRO_YEAR = 499000
MAX_YEAR = 1499000
T_PRO = int(taxable_of(Paise(PRO_YEAR)))
T_MAX = int(taxable_of(Paise(MAX_YEAR)))


def first_of_month_0200_ist(year: int, month: int) -> datetime:
    return datetime(year, month, 1, 2, 0, tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def balance(env: Env, code: str) -> int:
    value = await env.scalar(
        "SELECT b.balance_paise FROM billing.ledger_balances b "
        "JOIN billing.ledger_accounts a ON a.id = b.account_id WHERE a.code = :c",
        c=code,
    )
    return int(value or 0)  # type: ignore[call-overload]


async def unbalanced(env: Env) -> list[object]:
    return list(
        await env.sql(
            "SELECT transaction_id FROM billing.ledger_entries GROUP BY 1 "
            "HAVING sum(amount_paise) <> 0"
        )
    )


async def recognition_keys(env: Env) -> list[str]:
    rows = await env.sql(
        "SELECT idempotency_key FROM billing.ledger_transactions "
        "WHERE kind = 'revenue_recognition' ORDER BY occurred_at, idempotency_key"
    )
    return [r[0] for r in rows]


async def remaining(env: Env, payment_id: object) -> int:
    async with env.uow() as uow:
        payment = await uow.payments.get(payment_id)  # type: ignore[arg-type]
        assert payment is not None
        return int(await deferred_remaining(uow, payment))


async def first_payment(env: Env) -> object:
    return await env.scalar("SELECT id FROM billing.payments ORDER BY captured_at LIMIT 1")


# --- (a) every missing slice is posted -------------------------------------------------------------  # noqa: E501


async def test_a_skipped_month_is_caught_up_by_the_next_run(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    sub = await env.scalar("SELECT id FROM billing.subscriptions")
    payment = await first_payment(env)
    revenue = RevenueService(env.bctx.uow_factory, env.clock)

    env.clock.set(first_of_month_0200_ist(2027, 2))  # December and January were never run: n = 3
    assert await revenue.recognise() == 1
    assert await recognition_keys(env) == [f"recognise:{sub}:{payment}:{k}" for k in (1, 2, 3)]
    assert await balance(env, "revenue:subscription") == -sum(
        slice_for(Paise(T_PRO), k) for k in (1, 2, 3)
    )
    assert await revenue.recognise() == 0  # nothing new

    env.clock.set(first_of_month_0200_ist(2027, 4))  # n = 5: only slices 4 and 5 are missing
    assert await revenue.recognise() == 1
    assert len(await recognition_keys(env)) == 5
    assert await balance(env, "revenue:subscription") == -sum(
        slice_for(Paise(T_PRO), k) for k in range(1, 6)
    )
    assert await unbalanced(env) == []

    env.clock.set(first_of_month_0200_ist(2028, 6))  # long after: never more than 12 slices
    await revenue.recognise()
    assert len(await recognition_keys(env)) == 12
    assert await balance(env, "revenue:subscription") == -T_PRO
    assert await balance(env, "liability:deferred_revenue") == 0


async def test_a_partial_refund_caps_the_slices_so_deferred_never_goes_negative(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    payment = await first_payment(env)
    await issue_refund(
        env.bctx,
        {"payment_id": str(payment), "amount": 100000, "reason": "goodwill",
         "refund_key": "admin:k1", "interval": "year"},
    )  # fmt: skip
    credited = int(
        await env.scalar("SELECT taxable_paise FROM billing.invoices WHERE kind = 'credit_note'")
    )  # type: ignore[arg-type]
    revenue = RevenueService(env.bctx.uow_factory, env.clock)
    for month in [(2026, 12), *[(2027, m) for m in range(1, 12)]]:
        env.clock.set(first_of_month_0200_ist(*month))
        await revenue.recognise()
        assert await balance(env, "liability:deferred_revenue") <= 0  # never a debit balance
    assert await balance(env, "liability:deferred_revenue") == 0
    assert await balance(env, "revenue:subscription") == -(T_PRO - credited)
    assert await unbalanced(env) == []


# --- (b) release_deferred ---------------------------------------------------------------------------  # noqa: E501


async def test_release_posts_what_is_left_once_and_nothing_for_a_monthly_payment(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    payment_id = await first_payment(env)
    env.clock.set(first_of_month_0200_ist(2027, 1))
    await RevenueService(env.bctx.uow_factory, env.clock).recognise()  # slices 1 and 2
    done = sum(slice_for(Paise(T_PRO), k) for k in (1, 2))
    async with env.uow() as uow:
        payment = await uow.payments.get(payment_id)  # type: ignore[arg-type]
        assert payment is not None
        assert await release_deferred(uow, payment, env.clock.now()) == T_PRO - done
        assert await release_deferred(uow, payment, env.clock.now()) == 0  # second call: nothing
    assert await remaining(env, payment_id) == 0
    assert await balance(env, "liability:deferred_revenue") == 0
    assert await balance(env, "revenue:subscription") == -T_PRO
    keys = await recognition_keys(env)
    assert keys.count(f"release:{payment_id}") == 1


async def test_a_monthly_payment_has_nothing_to_release(env: Env) -> None:
    await env.activate()
    payment_id = await first_payment(env)
    async with env.uow() as uow:
        payment = await uow.payments.get(payment_id)  # type: ignore[arg-type]
        assert payment is not None
        assert await release_deferred(uow, payment, env.clock.now()) == 0
    assert await recognition_keys(env) == []


# --- (c) the moments that call it -----------------------------------------------------------------------  # noqa: E501


async def test_a_mid_year_upgrade_leaves_nothing_deferred_for_the_old_payment(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    old_payment = await first_payment(env)
    revenue = RevenueService(env.bctx.uow_factory, env.clock)
    env.clock.set(first_of_month_0200_ist(2027, 1))
    await revenue.recognise()  # two slices earned
    env.clock.set(datetime(2027, 2, 15, 4, 45, tzinfo=UTC))

    up = await env.service.change_plan(env.user, "max", Interval.YEAR)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_YEAR)
    await env.deliver(auth, charged)
    await env.process()
    assert await env.scalar("SELECT count(*) FROM billing.jobs WHERE kind = 'refund.issue'") == 1
    await env.run_jobs()  # cancels the old mandate, refunds the unused part, issues the credit note

    assert await remaining(env, old_payment) == 0
    credited = int(
        await env.scalar("SELECT taxable_paise FROM billing.invoices WHERE kind = 'credit_note'")
    )  # type: ignore[arg-type]
    assert credited > 0
    assert await balance(env, "liability:deferred_revenue") == -T_MAX  # only the new payment's
    assert await balance(env, "revenue:subscription") == -(T_PRO - credited)
    assert await unbalanced(env) == []
    # the new yearly period starts its own slices
    env.clock.set(first_of_month_0200_ist(2027, 3))
    await revenue.recognise()
    assert await balance(env, "revenue:subscription") == -(T_PRO - credited) - slice_for(
        Paise(T_MAX), 1
    )


async def test_an_upgrade_with_nothing_to_refund_releases_the_old_payment_at_once(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    old_payment = await first_payment(env)
    env.clock.set(await env.scalar("SELECT current_period_end FROM billing.subscriptions"))  # type: ignore[arg-type]
    up = await env.service.change_plan(env.user, "max", Interval.YEAR)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_YEAR)
    await env.deliver(auth, charged)
    await env.process()
    assert await env.scalar("SELECT count(*) FROM billing.jobs WHERE kind = 'refund.issue'") == 0
    assert await remaining(env, old_payment) == 0
    assert await balance(env, "revenue:subscription") == -T_PRO  # the whole old period was used
    assert await balance(env, "liability:deferred_revenue") == -T_MAX
    assert await unbalanced(env) == []


async def test_deleting_an_account_on_a_yearly_plan_releases_the_rest(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    payment = await first_payment(env)
    env.clock.set(first_of_month_0200_ist(2027, 2))
    await RevenueService(env.bctx.uow_factory, env.clock).recognise()  # three slices
    done = sum(slice_for(Paise(T_PRO), k) for k in (1, 2, 3))
    assert await balance(env, "liability:deferred_revenue") == -(T_PRO - done)

    assert await UserDeletionService(env.bctx, env.counters).apply(env.user, "evt-del") == "applied"
    assert await env.status() == "ended"
    assert await balance(env, "liability:deferred_revenue") == 0
    assert await balance(env, "revenue:subscription") == -T_PRO
    keys = await recognition_keys(env)
    assert f"release:{payment}" in keys and len(keys) == 4
    assert await unbalanced(env) == []
    await UserDeletionService(env.bctx, env.counters).apply(env.user, "evt-del")  # a replay
    assert len(await recognition_keys(env)) == 4


async def test_deleting_a_monthly_account_posts_no_release(env: Env) -> None:
    await env.activate()
    await UserDeletionService(env.bctx, env.counters).apply(env.user, "evt-del")
    assert await recognition_keys(env) == []


async def test_a_yearly_subscription_that_runs_to_its_end_releases_the_last_slice(env: Env) -> None:
    await env.activate("pro", PRO_YEAR, Interval.YEAR)
    await env.service.cancel(env.user)
    period_end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")
    env.clock.set(period_end)  # type: ignore[arg-type]
    await env.run_job("period.close")
    assert await env.status() == "ended"
    assert await balance(env, "liability:deferred_revenue") == 0
    assert await balance(env, "revenue:subscription") == -T_PRO
    assert await unbalanced(env) == []


async def test_a_halted_yearly_subscription_releases_too(env: Env) -> None:
    options = await env.activate("pro", PRO_YEAR, Interval.YEAR)
    env.clock.set(datetime(2027, 11, 1, 4, 45, tzinfo=UTC))
    await env.deliver(env.gateway.fail(options.gateway_subscription_id))
    await env.process()
    await env.deliver(env.gateway.halt(options.gateway_subscription_id))
    await env.process()
    assert await env.status() == "ended"
    assert await balance(env, "liability:deferred_revenue") == 0
    assert await balance(env, "revenue:subscription") == -T_PRO
