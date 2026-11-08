"""P1b: simulated renewals, webhook delivery with chaos, and recovery of dropped webhooks."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from billing.adapters.gateway_sim import SimulatorGateway
from billing.domain.catalog import Interval
from billing.jobs.handlers import sim as sim_jobs
from tests.integration.billing_env import STATE_BIHAR, Env, pump_webhooks

PAY = {"method": "card", "card_number": "4111111111111111", "otp": "123456"}


@pytest.fixture(autouse=True)
def _register_sim_jobs() -> None:
    sim_jobs.register()


def gw(env: Env) -> SimulatorGateway:
    assert isinstance(env.gateway, SimulatorGateway)
    return env.gateway


async def settle_webhooks(env: Env) -> None:
    """Send what is due, let the processor apply it (and repeat, as a worker would)."""
    for _ in range(3):
        await pump_webhooks(env)
        await env.process()


async def activate(env: Env, user: UUID | None = None) -> str:
    user = user or env.user
    options = await env.service.checkout(user, "pro", Interval.MONTH, STATE_BIHAR, "u@example.com")
    result = await gw(env).pay(options.gateway_subscription_id, **PAY)
    assert result.ok
    await settle_webhooks(env)
    return options.gateway_subscription_id


async def period_end(env: Env, gid: str) -> datetime:
    end = (await gw(env).fetch_subscription(gid)).current_end
    assert end is not None
    return end


async def counts(env: Env, user: UUID) -> tuple[str | None, int, int, str]:
    q = (
        "SELECT count(*) FROM billing.{t} x JOIN billing.billing_accounts a "
        "ON a.id = x.billing_account_id WHERE a.owner_id = :u"
    )
    payments = int(await env.scalar(q.format(t="payments"), u=user) or 0)
    invoices = int(await env.scalar(q.format(t="invoices"), u=user) or 0)
    return await env.status(user), payments, invoices, await env.plan_code(user)


async def sim_status(env: Env, gid: str) -> str:
    return (await gw(env).fetch_subscription(gid)).status


async def test_activation_through_the_webhook_path(sim_env: Env) -> None:
    gid = await activate(sim_env)
    assert await counts(sim_env, sim_env.user) == ("active", 1, 1, "pro")
    assert sim_env.user in sim_env.invalidated
    assert await sim_status(sim_env, gid) == "active"


async def test_renewal_success_charges_and_advances_the_period(sim_env: Env) -> None:
    gid = await activate(sim_env)
    first_end = await period_end(sim_env, gid)
    sim_env.clock.set(first_end)
    await sim_env.run_job("sim.renew")
    await settle_webhooks(sim_env)
    sub = await gw(sim_env).fetch_subscription(gid)
    assert sub.paid_count == 2 and sub.current_start == first_end and sub.current_end > first_end  # type: ignore[operator]
    assert await counts(sim_env, sim_env.user) == ("active", 2, 2, "pro")
    await sim_env.run_job("sim.renew")  # nothing is due any more
    assert (await gw(sim_env).fetch_subscription(gid)).paid_count == 2


async def test_renewal_failure_retries_three_times_then_halts(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    await g.set_next_outcome(gid, "fail")
    sim_env.clock.set(await period_end(sim_env, gid))
    seen = []
    await sim_env.run_job("sim.renew")
    seen.append(await sim_status(sim_env, gid))
    await settle_webhooks(sim_env)
    assert (await counts(sim_env, sim_env.user))[0] == "past_due"
    for _ in range(3):  # three retries, SIM_RETRY_INTERVAL (a day) apart
        await sim_env.run_job("sim.renew")  # too early: nothing happens
        sim_env.clock.advance(days=1)
        await sim_env.run_job("sim.renew")
        seen.append(await sim_status(sim_env, gid))
    assert seen == ["pending", "pending", "pending", "halted"]
    await settle_webhooks(sim_env)
    status, payments, _, _ = await counts(sim_env, sim_env.user)
    # Razorpay giving up (halted) ends the subscription: nothing was charged, access falls to Free
    assert (status, payments) == ("ended", 1)
    events = [r[0] for r in await sim_env.sql("SELECT event FROM billing_sim.sim_webhook_outbox")]
    assert events[-2:] == ["subscription.pending", "subscription.halted"]


async def test_a_failing_cycle_recovers_when_the_next_charge_succeeds(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    await g.set_next_outcome(gid, "fail")
    sim_env.clock.set(await period_end(sim_env, gid))
    await sim_env.run_job("sim.renew")
    await g.set_next_outcome(gid, "success")
    sim_env.clock.advance(days=1)
    await sim_env.run_job("sim.renew")
    await settle_webhooks(sim_env)
    assert await counts(sim_env, sim_env.user) == ("active", 2, 2, "pro")


async def test_halt_outcome_halts_at_once(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    await g.set_next_outcome(gid, "halt")
    sim_env.clock.set(await period_end(sim_env, gid))
    await sim_env.run_job("sim.renew")
    assert await sim_status(sim_env, gid) == "halted"
    await settle_webhooks(sim_env)
    assert await counts(sim_env, sim_env.user) == ("ended", 1, 1, "free")


async def test_paying_a_pending_subscription_restores_it(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    await g.set_next_outcome(gid, "fail")
    sim_env.clock.set(await period_end(sim_env, gid))
    await sim_env.run_job("sim.renew")
    await settle_webhooks(sim_env)
    assert (await counts(sim_env, sim_env.user))[0] == "past_due"
    assert (await g.pay(gid, **PAY)).ok  # the "Pay now" page
    await settle_webhooks(sim_env)
    assert await counts(sim_env, sim_env.user) == ("active", 2, 2, "pro")


async def test_cycle_end_cancel_cancels_instead_of_charging(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    await g.cancel_subscription(gid, at_cycle_end=True)
    sim_env.clock.set(await period_end(sim_env, gid))
    await sim_env.run_job("sim.renew")
    sub = await g.fetch_subscription(gid)
    assert sub.status == "cancelled" and sub.paid_count == 1


async def test_customer_cancel_sends_a_cancelled_webhook(sim_env: Env) -> None:
    gid = await activate(sim_env)
    await gw(sim_env).customer_cancel(gid)
    await settle_webhooks(sim_env)
    # a mandate cancelled outside the app lets the paid period run out, like a user's own cancel
    assert (await counts(sim_env, sim_env.user))[0] == "cancelling"


async def test_delivery_retries_with_backoff_and_records_delivered_at(sim_env: Env) -> None:
    g = gw(sim_env)
    options = await sim_env.checkout()
    await g.pay(options.gateway_subscription_id, **PAY)
    calls = []

    async def failing(body: bytes, headers: object) -> int:
        calls.append(1)
        return 500

    assert await g.deliver_due(failing) == 0  # all three fail and are rescheduled
    assert await g.deliver_due(failing) == 0  # not due yet: backoff
    sim_env.clock.advance(seconds=5)
    assert await pump_webhooks(sim_env) == 3
    rows = await sim_env.sql(
        "SELECT status, attempts, delivered_at, last_error FROM billing_sim.sim_webhook_outbox"
    )
    assert {r[0] for r in rows} == {"delivered"} and all(r[2] is not None for r in rows)
    assert all(r[1] == 2 for r in rows) and len(calls) == 3


async def test_dropped_webhooks_are_never_delivered(sim_env: Env) -> None:
    g = gw(sim_env)
    g.config = replace(g.config, drop_pct=100)
    await g.pay((await sim_env.checkout()).gateway_subscription_id, **PAY)
    sim_env.clock.advance(minutes=5)
    assert await pump_webhooks(sim_env) == 0
    statuses = {
        r[0] for r in await sim_env.sql("SELECT status FROM billing_sim.sim_webhook_outbox")
    }
    assert statuses == {"dropped"}


async def test_checkout_reconcile_recovers_a_dropped_checkout(sim_env: Env) -> None:
    g = gw(sim_env)
    g.config = replace(g.config, drop_pct=100)
    gid = (await sim_env.checkout()).gateway_subscription_id
    await g.pay(gid, **PAY)  # the user paid, and no webhook ever arrives
    await settle_webhooks(sim_env)
    assert (await counts(sim_env, sim_env.user))[0] == "incomplete"
    sim_env.clock.advance(minutes=20)
    await sim_env.run_job("checkout.reconcile")
    assert await counts(sim_env, sim_env.user) == ("active", 1, 1, "pro")


async def test_subscription_poll_recovers_a_dropped_renewal(sim_env: Env) -> None:
    g = gw(sim_env)
    gid = await activate(sim_env)
    g.config = replace(g.config, drop_pct=100)
    sim_env.clock.set(await period_end(sim_env, gid))
    await sim_env.run_job("sim.renew")
    await settle_webhooks(sim_env)
    assert await counts(sim_env, sim_env.user) == ("active", 1, 1, "pro")  # we never heard
    sim_env.clock.advance(hours=3)
    await sim_env.run_job("subscription.poll")
    assert await counts(sim_env, sim_env.user) == ("active", 2, 2, "pro")


async def scenario(env: Env, user: UUID) -> tuple[str | None, int, int, str]:
    """Activate, renew once, then fail the next renewal."""
    g = gw(env)
    gid = await activate(env, user)
    env.clock.set(await period_end(env, gid))
    await g.renew(gid)
    await settle_webhooks(env)
    await g.set_next_outcome(gid, "fail")
    env.clock.set(await period_end(env, gid))
    await g.renew(gid)
    await settle_webhooks(env)
    return await counts(env, user)


async def test_duplicates_and_reordering_leave_the_same_end_state(sim_env: Env) -> None:
    g = gw(sim_env)
    calm = await scenario(sim_env, uuid4())
    g.config = replace(g.config, duplicate_pct=100, reorder=True)
    noisy = await scenario(sim_env, uuid4())
    assert calm == noisy == ("past_due", 2, 2, "pro")
    dupes = await sim_env.scalar(
        "SELECT count(*) FROM (SELECT event_id FROM billing_sim.sim_webhook_outbox "
        "GROUP BY event_id HAVING count(*) > 1) d"
    )
    assert dupes and int(str(dupes)) > 0  # the chaos really happened
    inbox = await sim_env.scalar("SELECT count(*) FROM billing.webhook_inbox")
    sent = await sim_env.scalar(
        "SELECT count(DISTINCT event_id) FROM billing_sim.sim_webhook_outbox"
    )
    assert inbox == sent  # the inbox deduplicated every copy


async def test_sim_jobs_are_registered_only_by_register() -> None:
    from billing.jobs.runner import REGISTRY

    assert set(sim_jobs.HANDLERS) <= set(REGISTRY)  # this test module registered them
    assert {j.kind for j in sim_jobs.SCHEDULES} == set(sim_jobs.HANDLERS)


def test_fast_jobs_run_every_30_seconds() -> None:
    from billing.main_worker import SCHEDULE

    fast = {j.kind: j for j in sim_jobs.fast_schedule(list(SCHEDULE))}
    for kind in sim_jobs.FAST_KINDS:
        assert fast[kind].every is not None and fast[kind].every.total_seconds() == 30
    assert "sim.renew" not in {j.kind for j in SCHEDULE}  # not scheduled outside simulation
