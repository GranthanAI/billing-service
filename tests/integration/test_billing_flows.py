"""Phase 6 end to end: checkout, activation, renewal, failure, cancel, resume, plan changes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from billing.app.errors import (
    ChangePending,
    InvalidSignature,
    PlanNotAllowed,
    SubscriptionStateError,
)
from billing.domain.catalog import Interval
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env, config

OPEN_INVOICES = "SELECT number, kind, taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise FROM billing.invoices ORDER BY number"  # noqa: E501


async def unbalanced(env: Env) -> list[object]:
    return list(
        await env.sql(
            "SELECT transaction_id FROM billing.ledger_entries GROUP BY 1 HAVING sum(amount_paise) <> 0"  # noqa: E501
        )
    )


async def count(env: Env, table: str, where: str = "true") -> int:
    return int(await env.scalar(f"SELECT count(*) FROM billing.{table} WHERE {where}"))  # type: ignore[arg-type]


async def events(env: Env, kind: str) -> int:
    return await count(env, "outbox", f"event_type = '{kind}'")


# --- checkout and activation ---------------------------------------------------------------


async def test_happy_path_checkout_verify_webhook(env: Env) -> None:
    assert await env.plan_code() == "free"  # and caches the Free entitlement
    options = await env.checkout()
    assert options.status.value == "incomplete"
    assert options.key == "rzp_test_key"
    assert await env.status() == "incomplete"
    state = await env.scalar("SELECT state_code FROM billing.billing_accounts")
    assert state == "10"

    authenticated, charged, payment_id, signature = env.gateway.pay_checkout(
        options.gateway_subscription_id, PRO_MONTH
    )
    result = await env.service.verify_checkout(
        env.user, payment_id, options.gateway_subscription_id, signature
    )
    assert result.status.value == "active"
    assert await env.plan_code() == "pro"  # the cache was invalidated after commit
    assert env.invalidated == [env.user]

    await env.deliver(authenticated, charged)  # the webhooks land after verify: no-ops
    await env.process()

    assert await env.status() == "active"
    assert await count(env, "payments") == 1
    assert await count(env, "invoices") == 1
    rows = await env.sql(OPEN_INVOICES)
    assert [tuple(r) for r in rows] == [
        ("GA/2627/000001", "tax_invoice", 42288, 3806, 3806, 0, 49900)
    ]
    line = await env.scalar("SELECT description FROM billing.invoice_lines")
    assert line == "GranthanAI Pro · monthly · 1 Nov 2026 – 1 Dec 2026"
    assert await unbalanced(env) == []
    clearing = await env.scalar(
        "SELECT sum(e.amount_paise) FROM billing.ledger_entries e "
        "JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code = 'asset:razorpay_clearing'"  # noqa: E501
    )
    assert clearing == 49900
    assert await events(env, "subscription.changed") == 1
    assert await count(env, "subscription_history") == 1


async def test_bad_signature_is_refused(env: Env) -> None:
    options = await env.checkout()
    with pytest.raises(InvalidSignature):
        await env.service.verify_checkout(env.user, "pay_x", options.gateway_subscription_id, "00")


@pytest.mark.parametrize("order", ["verify_first", "webhook_first", "parallel"])
async def test_verify_and_webhook_race(env: Env, order: str) -> None:
    options = await env.checkout()
    authenticated, charged, payment_id, signature = env.gateway.pay_checkout(
        options.gateway_subscription_id, PRO_MONTH
    )

    async def verify() -> None:
        await env.service.verify_checkout(
            env.user, payment_id, options.gateway_subscription_id, signature
        )

    async def webhooks() -> None:
        await env.deliver(authenticated, charged)
        await env.process()

    if order == "verify_first":
        await verify()
        await webhooks()
    elif order == "webhook_first":
        await webhooks()
        await verify()
    else:
        await asyncio.gather(verify(), webhooks())
        await webhooks()
    assert await env.status() == "active"
    assert await count(env, "payments") == 1
    assert await count(env, "invoices") == 1
    assert await count(env, "subscription_history") == 1
    assert await events(env, "subscription.changed") == 1


async def test_duplicate_and_out_of_order_webhooks_end_in_the_same_state(env: Env) -> None:
    options = await env.checkout()
    authenticated, charged, _, _ = env.gateway.pay_checkout(
        options.gateway_subscription_id, PRO_MONTH
    )
    # charged before authenticated, every delivery twice (same event id)
    await env.deliver(charged, charged.again(), authenticated, authenticated.again())
    assert await count(env, "webhook_inbox") == 2
    await env.process()
    assert await env.status() == "active"
    assert await count(env, "payments") == 1 and await count(env, "invoices") == 1
    assert await count(env, "subscription_history") == 1

    # Razorpay redelivering the charge under a new event id still cannot double-charge
    copy = env.gateway._webhook(
        "subscription_charged",
        subscription={"id": options.gateway_subscription_id},
        payment=charged.payload()["payload"]["payment"]["entity"],
    )
    await env.deliver(copy)
    await env.process()
    assert await count(env, "payments") == 1 and await count(env, "invoices") == 1
    assert await count(env, "ledger_transactions", "kind = 'subscription_charge'") == 1


async def test_checkout_reuses_an_incomplete_subscription(env: Env) -> None:
    first = await env.checkout()
    second = await env.checkout()
    assert second.subscription_id == first.subscription_id
    assert second.gateway_subscription_id == first.gateway_subscription_id
    assert len(env.gateway.called("create_subscription")) == 1
    other = await env.checkout("max")
    assert other.subscription_id != first.subscription_id


async def test_checkout_refuses_free_org_and_live_subscriptions(env: Env) -> None:
    for code in ("free", "org_pro", "payg"):
        with pytest.raises(PlanNotAllowed):
            await env.checkout(code)
    await env.activate()
    with pytest.raises(SubscriptionStateError):
        await env.checkout("max")


# --- renewal, failure ----------------------------------------------------------------------


async def test_renewal_advances_the_period_and_numbers_the_next_invoice(env: Env) -> None:
    options = await env.activate()
    env.clock.set(datetime(2026, 12, 1, 4, 45, tzinfo=UTC))
    await env.deliver(env.gateway.charge(options.gateway_subscription_id, PRO_MONTH))
    await env.process()
    numbers = [r[0] for r in await env.sql(OPEN_INVOICES)]
    assert numbers == ["GA/2627/000001", "GA/2627/000002"]
    end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")
    assert end == datetime(2027, 1, 1, 4, 45, tzinfo=UTC)
    assert await events(env, "subscription.changed") == 1  # a plain renewal changes nothing


async def test_failed_charge_grace_recovery_and_halt(env: Env) -> None:
    options = await env.activate()
    gid = options.gateway_subscription_id
    env.clock.set(datetime(2026, 12, 1, 4, 45, tzinfo=UTC))
    await env.deliver(env.gateway.fail(gid))
    await env.process()
    assert await env.status() == "past_due"
    assert await events(env, "payment.failed") == 1
    grace = await env.scalar("SELECT grace_until FROM billing.subscriptions")
    assert grace == env.clock.now() + timedelta(days=3)
    assert await env.plan_code() == "pro"  # access kept during grace

    env.clock.advance(hours=6)
    await env.deliver(env.gateway.charge(gid, PRO_MONTH))
    await env.process()
    assert await env.status() == "active"
    assert await env.scalar("SELECT grace_until FROM billing.subscriptions") is None

    env.clock.set(datetime(2027, 1, 1, 4, 45, tzinfo=UTC))
    await env.deliver(env.gateway.fail(gid))
    await env.process()
    await env.deliver(env.gateway.halt(gid))
    await env.process()
    assert await env.status() == "ended"
    assert await env.plan_code() == "free"
    assert await count(env, "jobs", "kind = 'gateway.cancel'") == 1


async def test_out_of_date_failure_event_is_ignored(env: Env) -> None:
    options = await env.activate()
    gid = options.gateway_subscription_id
    env.clock.set(datetime(2026, 12, 1, 4, 45, tzinfo=UTC))
    late_pending = env.gateway.fail(gid)
    await env.deliver(env.gateway.charge(gid, PRO_MONTH))  # Razorpay recovered meanwhile
    await env.deliver(late_pending)
    await env.process()
    assert await env.status() == "active"


# --- cancel and resume ---------------------------------------------------------------------


async def test_cancel_then_resume(env: Env) -> None:
    options = await env.activate()
    old = options.gateway_subscription_id
    cancelled = await env.service.cancel(env.user)
    assert cancelled.status.value == "cancelling"
    assert await env.plan_code() == "pro"  # access kept to the period end
    job = await env.sql("SELECT payload FROM billing.jobs WHERE kind = 'gateway.cancel'")
    assert job[0][0] == {"gateway_subscription_id": old, "at_cycle_end": True}
    await env.run_jobs()
    assert ("cancel_subscription", (old, True)) in env.gateway.calls
    with pytest.raises(SubscriptionStateError):
        await env.service.cancel(env.user)  # cancel twice

    resumed = await env.service.resume(env.user)
    new = resumed.gateway_subscription_id
    period_end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")
    assert env.gateway.subscriptions[new].start_at == period_end
    with pytest.raises(ChangePending):
        await env.service.resume(env.user)

    await env.deliver(env.gateway.authenticate(new))
    await env.process()
    assert await env.status() == "active"
    nxt = await env.scalar("SELECT next_gateway_subscription_id FROM billing.subscriptions")
    assert nxt == new
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "authorised"

    env.clock.set(period_end)  # type: ignore[arg-type]
    await env.deliver(env.gateway.cancel_from_customer(old))  # the old mandate ending is the swap
    await env.deliver(env.gateway.charge(new, PRO_MONTH))
    await env.process()
    row = (
        await env.sql(
            "SELECT gateway_subscription_id, next_gateway_subscription_id, status::text FROM billing.subscriptions"  # noqa: E501
        )
    )[0]
    assert tuple(row) == (new, None, "active")
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "applied"
    assert await count(env, "invoices") == 2


async def test_customer_cancelling_in_the_upi_app(env: Env) -> None:
    options = await env.activate()
    before = len(env.gateway.calls)
    await env.deliver(env.gateway.cancel_from_customer(options.gateway_subscription_id))
    await env.process()
    assert await env.status() == "cancelling"
    assert not [c for c in env.gateway.calls[before:] if c[0] == "cancel_subscription"]
    assert await count(env, "jobs", "kind = 'gateway.cancel'") == 0
    assert await env.plan_code() == "pro"
    # a second cancelled event, now illegal, is ignored
    await env.deliver(env.gateway.cancel_from_customer(options.gateway_subscription_id))
    await env.process()
    assert await env.status() == "cancelling"


# --- plan changes --------------------------------------------------------------------------


async def test_upgrade_pro_to_max_with_refund_and_credit_note(env: Env) -> None:
    options = await env.activate()
    old = options.gateway_subscription_id
    env.clock.set(datetime(2026, 11, 16, 4, 45, tzinfo=UTC))  # 15 of 30 days left

    upgrade = await env.service.change_plan(env.user, "max", Interval.MONTH)
    assert upgrade.kind == "upgrade"
    assert env.gateway.subscriptions[upgrade.gateway_subscription_id].start_at is None
    with pytest.raises(ChangePending):
        await env.service.change_plan(env.user, "max", Interval.MONTH)
    assert await env.plan_code() == "pro"  # nothing changes until the new mandate pays

    auth, charged, _, _ = env.gateway.pay_checkout(upgrade.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    assert await env.plan_code() == "max"  # right after commit, before any job ran
    row = (
        await env.sql(
            "SELECT gateway_subscription_id, current_period_start, current_period_end FROM billing.subscriptions"  # noqa: E501
        )
    )[0]
    assert row[0] == upgrade.gateway_subscription_id
    assert row[1] == datetime(2026, 11, 16, 4, 45, tzinfo=UTC)
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "applied"

    await env.run_jobs()
    assert ("cancel_subscription", (old, False)) in env.gateway.calls  # immediately
    assert env.gateway.subscriptions[old].status == "cancelled"
    refunded = [c for c in env.gateway.calls if c[0] == "refund_payment"]
    assert len(refunded) == 1 and refunded[0][1][1] == 24950  # type: ignore[index]

    note = (
        await env.sql(
            "SELECT number, taxable_paise, cgst_paise, sgst_paise, total_paise, kind FROM billing.invoices WHERE kind = 'credit_note'"  # noqa: E501
        )
    )[0]
    assert tuple(note) == ("CN/2627/000001", 21144, 1903, 1903, 24950, "credit_note")
    assert (
        await env.scalar("SELECT status FROM billing.payments WHERE amount_paise = 49900")
        == "partially_refunded"
    )
    assert await count(env, "refunds") == 1
    assert await unbalanced(env) == []
    assert await count(env, "ledger_transactions", "kind = 'subscription_refund'") == 1
    clearing = await env.scalar(
        "SELECT sum(e.amount_paise) FROM billing.ledger_entries e "
        "JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code = 'asset:razorpay_clearing'"  # noqa: E501
    )
    assert clearing == PRO_MONTH + MAX_MONTH - 24950

    # the webhook for the refund only updates its status
    refund = next(iter(env.gateway.refunds.values()))
    await env.deliver(env.gateway.refund_processed(refund))
    await env.process()
    assert await env.scalar("SELECT status FROM billing.refunds") == "processed"


async def test_downgrade_max_to_pro_applies_at_the_boundary(env: Env) -> None:
    options = await env.activate("max", MAX_MONTH)
    old = options.gateway_subscription_id
    period_end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")

    down = await env.service.change_plan(env.user, "pro", Interval.MONTH)
    assert down.kind == "downgrade"
    new = down.gateway_subscription_id
    assert env.gateway.subscriptions[new].start_at == period_end
    assert await count(env, "payments") == 1  # nothing charged now

    await env.deliver(env.gateway.authenticate(new))
    await env.process()
    row = (
        await env.sql(
            "SELECT next_gateway_subscription_id, pending_plan_version_id IS NOT NULL FROM billing.subscriptions"  # noqa: E501
        )
    )[0]
    assert tuple(row) == (new, True)
    assert await env.plan_code() == "max"
    await env.run_jobs()
    assert ("cancel_subscription", (old, True)) in env.gateway.calls

    env.clock.set(period_end)  # type: ignore[arg-type]
    await env.deliver(env.gateway.charge(new, PRO_MONTH))
    await env.process()
    assert await env.plan_code() == "pro"
    row = (
        await env.sql(
            "SELECT gateway_subscription_id, next_gateway_subscription_id, pending_plan_version_id FROM billing.subscriptions"  # noqa: E501
        )
    )[0]
    assert tuple(row) == (new, None, None)
    description = await env.scalar(
        "SELECT description FROM billing.invoice_lines ORDER BY invoice_id DESC LIMIT 1"
    )
    assert "Pro" in str(description)


async def test_change_pending_and_same_plan(env: Env) -> None:
    await env.activate()
    with pytest.raises(PlanNotAllowed):
        await env.service.change_plan(env.user, "pro", Interval.MONTH)
    await env.service.change_plan(env.user, "max", Interval.MONTH)
    with pytest.raises(ChangePending):
        await env.service.change_plan(env.user, "max", Interval.YEAR)
    # cancelling abandons the waiting mandate
    await env.service.cancel(env.user)
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "abandoned"


async def test_change_needs_an_active_subscription(env: Env) -> None:
    with pytest.raises(SubscriptionStateError):
        await env.service.change_plan(env.user, "max", Interval.MONTH)


# --- invoices ------------------------------------------------------------------------------


async def test_unregistered_supplier_issues_a_bill_of_supply(env: Env) -> None:
    env.with_config(config(gst_registered=False))
    await env.activate()
    row = (await env.sql(OPEN_INVOICES))[0]
    assert tuple(row) == ("BS/2627/000001", "bill_of_supply", 49900, 0, 0, 0, 49900)
    assert await unbalanced(env) == []
    gst = await env.scalar(
        "SELECT count(*) FROM billing.ledger_entries e JOIN billing.ledger_accounts a "
        "ON a.id = e.account_id WHERE a.code LIKE 'liability:gst_output%'"
    )
    assert gst == 0


async def test_other_state_customer_pays_igst(env: Env) -> None:
    await env.service.checkout(
        env.user, "pro", Interval.MONTH, "29", "user@example.com", "Acme", "29ABCDE1234F1Z5"
    )
    sub = env.gateway.subscriptions[next(iter(env.gateway.subscriptions))]
    auth, charged, _, _ = env.gateway.pay_checkout(sub.id, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    row = (await env.sql(OPEN_INVOICES))[0]
    assert tuple(row) == ("GA/2627/000001", "tax_invoice", 42288, 0, 0, 7612, 49900)
    who = (
        await env.sql(
            "SELECT customer_name, customer_gstin, customer_state_code, supplier_state_code FROM billing.invoices"  # noqa: E501
        )
    )[0]
    assert tuple(who) == ("Acme", "29ABCDE1234F1Z5", "29", "10")


async def test_financial_year_rolls_over_on_1_april_ist(env: Env) -> None:
    from uuid import uuid4

    env.clock.set(datetime(2027, 3, 31, 18, 29, tzinfo=UTC))  # 23:59 IST on 31 March
    await env.activate()
    env.clock.set(datetime(2027, 3, 31, 18, 31, tzinfo=UTC))  # 00:01 IST on 1 April
    other = uuid4()
    options = await env.checkout(user=other)
    auth, charged, _, _ = env.gateway.pay_checkout(options.gateway_subscription_id, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    numbers = [r[0] for r in await env.sql(OPEN_INVOICES)]
    assert numbers == ["GA/2627/000001", "GA/2728/000001"]


# --- reconcile -----------------------------------------------------------------------------


async def run_reconcile(env: Env) -> None:
    async with env.uow() as uow:
        await uow.jobs.enqueue("checkout.reconcile", {}, env.clock.now(), f"rec:{env.clock.now()}")
    await env.run_jobs()


async def test_reconcile_activates_a_checkout_whose_webhook_was_missed(env: Env) -> None:
    options = await env.checkout()
    env.gateway.pay_checkout(options.gateway_subscription_id, PRO_MONTH)  # webhooks never delivered
    await run_reconcile(env)
    assert await env.status() == "incomplete"  # younger than 15 minutes: left alone
    env.clock.advance(minutes=20)
    await run_reconcile(env)
    assert await env.status() == "active"
    assert await count(env, "payments") == 1 and await count(env, "invoices") == 1
    assert await env.plan_code() == "pro"


async def test_reconcile_ends_an_abandoned_checkout_and_cancels_it(env: Env) -> None:
    options = await env.checkout()
    env.clock.advance(hours=25)
    await run_reconcile(env)
    await env.run_jobs()
    assert await env.status() == "ended"
    assert env.gateway.subscriptions[options.gateway_subscription_id].status == "cancelled"


async def test_reconcile_abandons_a_stale_change(env: Env) -> None:
    await env.activate()
    change = await env.service.change_plan(env.user, "max", Interval.MONTH)
    env.clock.advance(hours=25)
    await run_reconcile(env)
    await env.run_jobs()
    assert await env.scalar("SELECT status FROM billing.subscription_changes") == "abandoned"
    assert env.gateway.subscriptions[change.gateway_subscription_id].status == "cancelled"
    assert await env.status() == "active"


# --- processor -----------------------------------------------------------------------------


async def test_processor_backs_off_then_fails_after_ten_attempts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from billing.observability import WEBHOOK_FAILED

    options = await env.checkout()
    auth, charged, _, _ = env.gateway.pay_checkout(options.gateway_subscription_id, PRO_MONTH)

    async def boom(_: str) -> object:
        raise RuntimeError("razorpay is down")

    monkeypatch.setattr(env.gateway, "fetch_subscription", boom)
    await env.deliver(charged)
    failed_before = WEBHOOK_FAILED._value.get()
    waits: list[float] = []
    for attempt in range(1, 11):
        assert await env.process() == 1
        row = (await env.sql("SELECT attempts, status, next_try_at FROM billing.webhook_inbox"))[0]
        assert row[0] == attempt
        if attempt < 10:
            assert row[1] == "received"
            waits.append((row[2] - env.clock.now()).total_seconds())
            env.clock.advance(seconds=waits[-1] + 1)
    assert waits[:4] == [30, 60, 120, 240]
    assert waits[-1] == 3600  # capped at an hour
    assert row[1] == "failed"
    assert WEBHOOK_FAILED._value.get() == failed_before + 1
    assert await env.process() == 0
    assert await env.status() == "incomplete"


async def test_reprocessing_a_committed_charge_does_nothing_more(env: Env) -> None:
    options = await env.activate()
    upgrade_at = datetime(2026, 11, 16, 4, 45, tzinfo=UTC)
    env.clock.set(upgrade_at)
    up = await env.service.change_plan(env.user, "max", Interval.MONTH)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    snapshot = (
        await count(env, "payments"),
        await count(env, "invoices"),
        await count(env, "ledger_transactions"),
        await count(env, "subscription_history"),
        await count(env, "jobs", "kind = 'refund.issue'"),
        await events(env, "subscription.changed"),
    )
    # the worker "crashed" before the inbox rows were marked: they are due again
    await env.sql("SELECT 1")
    async with env.migrator_engine.begin() as conn:
        import sqlalchemy as sa

        await conn.execute(sa.text("UPDATE billing.webhook_inbox SET status = 'received'"))
    env.clock.advance(minutes=6)  # past the 5-minute lease the first pass left on the rows
    assert await env.process() == 4
    again = (
        await count(env, "payments"),
        await count(env, "invoices"),
        await count(env, "ledger_transactions"),
        await count(env, "subscription_history"),
        await count(env, "jobs", "kind = 'refund.issue'"),
        await events(env, "subscription.changed"),
    )
    assert again == snapshot
    assert options.subscription_id is not None
