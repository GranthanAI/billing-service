"""F2: the preview and the real plan change / resume give the same amounts and dates (one source of truth)."""

from datetime import UTC, datetime

import pytest

from billing.api.rest.billing import checkout_json
from billing.app.errors import SubscriptionStateError
from billing.domain.calendar import add_months
from billing.domain.catalog import Interval
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env

HALF = datetime(2026, 11, 16, 4, 45, tzinfo=UTC)  # 15 of the 30 days of Pro's first period are left
PRO_YEAR = 499000


async def count(env: Env, table: str) -> int:
    return int(await env.scalar(f"SELECT count(*) FROM billing.{table}"))  # type: ignore[arg-type]


async def test_upgrade_mid_period_preview_equals_the_real_change_and_the_refund(env: Env) -> None:
    await env.activate()
    env.clock.set(HALF)
    calls_before = len(env.gateway.calls)

    q = await env.service.preview_change(env.user, "max", Interval.MONTH)
    assert (q.kind, q.amount_due_now_paise, q.refund_paise, q.price_paise) == (
        "upgrade",
        MAX_MONTH,
        24950,
        MAX_MONTH,
    )
    assert q.effective_at == HALF and q.new_period_end == add_months(HALF, 1)
    assert (
        len(env.gateway.calls) == calls_before and await count(env, "subscription_changes") == 0
    )  # no side effects

    options = await env.service.change_plan(env.user, "max", Interval.MONTH)
    assert options.quote == q
    body = checkout_json(options)
    assert (
        body["kind"] == "upgrade"
        and body["amount_due_now_paise"] == MAX_MONTH
        and body["refund_paise"] == 24950
    )
    assert body["effective_at"] == HALF.isoformat() and body["price_paise"] == MAX_MONTH
    assert body["new_period_end"] == add_months(HALF, 1).isoformat()

    auth, charged, _, _ = env.gateway.pay_checkout(options.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    await env.run_jobs()
    refunded = [c for c in env.gateway.calls if c[0] == "refund_payment"]
    assert len(refunded) == 1 and refunded[0][1][1] == q.refund_paise  # type: ignore[index]  # what was previewed is what went back


async def test_downgrade_starts_at_the_period_end_and_costs_nothing_now(env: Env) -> None:
    await env.activate("max", MAX_MONTH)
    period_end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")
    q = await env.service.preview_change(env.user, "pro", Interval.MONTH)
    assert (q.kind, q.amount_due_now_paise, q.refund_paise, q.price_paise) == (
        "downgrade",
        0,
        0,
        PRO_MONTH,
    )
    assert q.effective_at == period_end and q.new_period_end == add_months(period_end, 1)  # type: ignore[arg-type]
    options = await env.service.change_plan(env.user, "pro", Interval.MONTH)
    assert options.quote == q
    assert env.gateway.subscriptions[options.gateway_subscription_id].start_at == q.effective_at


async def test_an_interval_change_is_labelled_and_an_upgrade_in_money(env: Env) -> None:
    await env.activate()
    env.clock.set(HALF)
    q = await env.service.preview_change(env.user, "pro", Interval.YEAR)
    assert q.kind == "interval_change"
    assert (q.amount_due_now_paise, q.refund_paise, q.price_paise) == (PRO_YEAR, 24950, PRO_YEAR)
    assert q.effective_at == HALF and q.new_period_end == add_months(HALF, 12)
    assert (await env.service.change_plan(env.user, "pro", Interval.YEAR)).quote == q


async def test_resume_is_authorised_now_and_charged_at_the_period_end(env: Env) -> None:
    await env.activate()
    await env.service.cancel(env.user)
    period_end = await env.scalar("SELECT current_period_end FROM billing.subscriptions")
    q = await env.service.preview_resume(env.user)
    assert (q.kind, q.amount_due_now_paise, q.refund_paise, q.price_paise) == (
        "resume",
        0,
        0,
        PRO_MONTH,
    )
    assert q.effective_at == period_end and q.new_period_end == add_months(period_end, 1)  # type: ignore[arg-type]
    assert await count(env, "subscription_changes") == 0
    options = await env.service.resume(env.user)
    assert options.quote == q and checkout_json(options)["kind"] == "resume"


async def test_a_preview_applies_every_check_of_the_real_change(env: Env) -> None:
    with pytest.raises(SubscriptionStateError):
        await env.service.preview_change(env.user, "max", Interval.MONTH)  # no subscription yet
    await env.activate()
    with pytest.raises(SubscriptionStateError):
        await env.service.preview_resume(env.user)  # only a cancelling subscription resumes
