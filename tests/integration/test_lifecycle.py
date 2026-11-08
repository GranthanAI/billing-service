"""Stage D: scheduled jobs and the subscription lifecycle, on a test clock."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from billing.app.lifecycle import RateLimiter
from billing.domain.catalog import Interval
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env

DEC1 = datetime(2026, 12, 1, 4, 45, tzinfo=UTC)  # the end of the first monthly period


async def one(env: Env, query: str, **params: object) -> object:
    return await env.scalar(query, **params)


async def sub_row(env: Env) -> sa.Row[tuple[object, ...]]:
    return (
        await env.sql(
            "SELECT status::text, grace_until, current_period_end, gateway_subscription_id, "
            "next_gateway_subscription_id, pending_plan_version_id FROM billing.subscriptions"
        )
    )[0]


async def event_types(env: Env) -> list[str]:
    rows = await env.sql("SELECT event_type FROM billing.outbox ORDER BY id")
    return [r[0] for r in rows]


async def event_bodies(env: Env, kind: str) -> list[dict[str, object]]:
    rows = await env.sql(
        "SELECT payload FROM billing.outbox WHERE event_type = :k ORDER BY id", k=kind
    )
    return [json.loads(bytes(r[0])) for r in rows]


# --- grace.expire ----------------------------------------------------------------------------


async def test_grace_expiry_suspends_and_a_late_charge_restores(env: Env) -> None:
    options = await env.activate()
    gid = options.gateway_subscription_id
    env.clock.set(DEC1)
    await env.deliver(env.gateway.fail(gid))
    await env.process()
    assert (await sub_row(env))[0] == "past_due"

    await env.run_job("grace.expire")  # grace runs to Dec 4 04:45
    assert (await sub_row(env))[0] == "past_due"
    env.clock.advance(days=3, minutes=1)
    await env.run_job("grace.expire")
    assert (await sub_row(env))[0] == "suspended"
    assert await env.plan_code() == "free"  # entitlements fall to Free
    suspensions = "SELECT count(*) FROM billing.subscription_history WHERE to_status = 'suspended'"
    assert await one(env, suspensions) == 1
    await env.run_job("grace.expire")  # twice is the same as once
    assert await one(env, suspensions) == 1
    assert await events_of(env, "subscription.changed") == 2  # active, then suspended

    await env.deliver(env.gateway.charge(gid, PRO_MONTH))  # Razorpay's retry finally works
    await env.process()
    assert (await sub_row(env))[0] == "active"
    assert await env.plan_code() == "pro"


async def events_of(env: Env, kind: str) -> int:
    return len(await event_bodies(env, kind))


# --- period.close ----------------------------------------------------------------------------


async def test_period_close_ends_a_cancelling_subscription_at_the_period_end(env: Env) -> None:
    await env.activate()
    await env.service.cancel(env.user)
    end = (await sub_row(env))[2]
    assert isinstance(end, datetime)
    env.clock.set(end - timedelta(minutes=1))
    await env.run_job("period.close")
    assert (await sub_row(env))[0] == "cancelling"
    assert await env.plan_code() == "pro"  # access kept to the very end
    env.clock.set(end)
    await env.run_job("period.close")
    await env.run_job("period.close")  # idempotent
    assert (await sub_row(env))[0] == "ended"
    assert await env.plan_code() == "free"
    assert (
        await one(
            env, "SELECT count(*) FROM billing.subscription_history WHERE to_status = 'ended'"
        )
        == 1
    )
    assert (await event_bodies(env, "subscription.changed"))[-1]["status"] == "ended"


# --- the next mandate failing ----------------------------------------------------------------


async def start_downgrade(env: Env) -> str:
    options = await env.activate("max", MAX_MONTH)
    down = await env.service.change_plan(env.user, "pro", Interval.MONTH)
    await env.deliver(env.gateway.authenticate(down.gateway_subscription_id))
    await env.process()
    assert options.gateway_subscription_id != down.gateway_subscription_id
    return down.gateway_subscription_id


async def test_a_failed_first_charge_of_the_next_mandate_is_past_due_on_the_new_plan(
    env: Env,
) -> None:
    new = await start_downgrade(env)
    env.clock.set(DEC1)
    await env.deliver(env.gateway.fail(new))
    await env.process()
    row = await sub_row(env)
    assert row[0] == "past_due" and row[3] == new and row[4] is None and row[5] is None
    assert row[1] == DEC1 + timedelta(days=3)
    assert await env.plan_code() == "pro"  # the plan it was moving to; grace keeps access
    failed = await event_bodies(env, "payment.failed")
    assert len(failed) == 1 and failed[0]["amount_paise"] == PRO_MONTH
    assert (await event_bodies(env, "subscription.changed"))[-1]["plan_code"] == "pro"

    await env.deliver(env.gateway.charge(new, PRO_MONTH))  # recovers on the new mandate
    await env.process()
    assert (await sub_row(env))[0] == "active"


async def test_a_halted_next_mandate_ends_the_subscription(env: Env) -> None:
    new = await start_downgrade(env)
    env.clock.set(DEC1)
    await env.deliver(env.gateway.halt(new))  # no `pending` event reached us first
    await env.process()
    assert (await sub_row(env))[0] == "ended"


# --- subscription.poll -----------------------------------------------------------------------


async def test_the_poll_recovers_a_missed_charge(env: Env) -> None:
    options = await env.activate()
    gid = options.gateway_subscription_id
    env.clock.set(DEC1 + timedelta(hours=1))
    env.gateway.charge(gid, PRO_MONTH)  # the renewal happened; its webhook was lost
    await env.run_job("subscription.poll")
    assert await one(env, "SELECT count(*) FROM billing.payments") == 1  # only 1 h late: left alone

    env.clock.set(DEC1 + timedelta(hours=3))
    await env.run_job("subscription.poll")
    assert await one(env, "SELECT count(*) FROM billing.payments") == 2
    assert await one(env, "SELECT count(*) FROM billing.invoices") == 2
    assert (await sub_row(env))[2] == datetime(2027, 1, 1, 4, 45, tzinfo=UTC)
    await env.run_job("subscription.poll")  # nothing left to recover
    assert await one(env, "SELECT count(*) FROM billing.payments") == 2


@pytest.mark.parametrize("failure", ["pending", "halted"])
async def test_the_poll_recovers_missed_failures(env: Env, failure: str) -> None:
    options = await env.activate()
    gid = options.gateway_subscription_id
    env.clock.set(DEC1 + timedelta(hours=3))
    env.gateway.fail(gid)  # webhooks lost
    if failure == "halted":
        env.gateway.halt(gid)
    await env.run_job("subscription.poll")
    assert (await sub_row(env))[0] == ("past_due" if failure == "pending" else "ended")
    assert await events_of(env, "payment.failed") == 1
    await env.run_job("subscription.poll")  # again: the same
    assert await events_of(env, "payment.failed") == 1


async def test_the_poll_recovers_a_cancellation_from_the_customers_app(env: Env) -> None:
    options = await env.activate()
    env.clock.set(DEC1 + timedelta(hours=3))
    env.gateway.cancel_from_customer(options.gateway_subscription_id)  # webhook lost
    await env.run_job("subscription.poll")
    assert (await sub_row(env))[0] == "cancelling"


async def test_the_poll_leaves_healthy_subscriptions_alone(env: Env) -> None:
    await env.activate()
    before = len(env.gateway.calls)
    await env.run_job("subscription.poll")  # period not over: no Razorpay calls at all
    assert len(env.gateway.calls) == before


async def test_the_rate_limiter_spaces_calls_to_five_a_second() -> None:
    now = [100.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(round(seconds, 3))
        now[0] += seconds

    limiter = RateLimiter(5, sleep=sleep, monotonic=lambda: now[0])
    for _ in range(6):
        await limiter.wait()
    assert waits == [0.2] * 5  # the first call goes at once, then one every 0.2 s: 5 a second
    await limiter.wait(3)  # a call that costs three slots...
    await limiter.wait()  # ...holds back the next one for those three
    assert waits[-2:] == [0.2, 0.6]


# --- renewal.remind --------------------------------------------------------------------------


async def test_renewal_reminder_fires_once_per_period(env: Env) -> None:
    await env.activate()  # period ends Dec 1 04:45
    env.clock.set(DEC1 - timedelta(hours=30))
    await env.run_job("renewal.remind")
    await env.run_job("renewal.remind")  # the same day again
    env.clock.set(DEC1 - timedelta(hours=26))  # still inside the 24-48 h window
    await env.run_job("renewal.remind")
    events = await event_bodies(env, "subscription.renewal_upcoming")
    assert len(events) == 1
    assert events[0]["plan_code"] == "pro" and events[0]["amount_paise"] == PRO_MONTH
    assert events[0]["charge_at"] == "2026-12-01T04:45:00Z"
    assert events[0]["owner_id"] == str(env.user)
    env.clock.set(DEC1 - timedelta(hours=10))  # too close now
    await env.run_job("renewal.remind")
    assert await events_of(env, "subscription.renewal_upcoming") == 1

    options = await env.service.change_plan(env.user, "max", Interval.MONTH)  # an open upgrade
    assert options.kind == "upgrade"

    # the next period gets its own reminder
    env.clock.set(DEC1)
    sub = (await env.sql("SELECT gateway_subscription_id FROM billing.subscriptions"))[0][0]
    await env.deliver(env.gateway.charge(sub, PRO_MONTH))
    await env.process()
    env.clock.set(datetime(2027, 1, 1, 4, 45, tzinfo=UTC) - timedelta(hours=30))
    await env.run_job("renewal.remind")
    assert await events_of(env, "subscription.renewal_upcoming") == 2


async def test_renewal_reminder_skips_cancelling_subscriptions_and_names_a_scheduled_downgrade(
    env: Env,
) -> None:
    await env.activate("max", MAX_MONTH)
    down = await env.service.change_plan(env.user, "pro", Interval.MONTH)
    await env.deliver(env.gateway.authenticate(down.gateway_subscription_id))
    await env.process()
    env.clock.set(DEC1 - timedelta(hours=30))
    await env.run_job("renewal.remind")
    (event,) = await event_bodies(env, "subscription.renewal_upcoming")
    assert (
        event["plan_code"] == "pro" and event["amount_paise"] == PRO_MONTH
    )  # what will be charged

    other = await env.checkout(user=uuid4())
    assert other.status.value == "incomplete"  # not active: never reminded
    await env.service.cancel(env.user)
    env.clock.set(DEC1 - timedelta(hours=29))
    await env.run_job("renewal.remind")
    assert await events_of(env, "subscription.renewal_upcoming") == 1


# --- housekeeping ----------------------------------------------------------------------------


async def test_housekeeping_removes_only_what_is_past_its_window(env: Env) -> None:
    now = env.clock.now()

    async def put(sql: str, **params: object) -> None:
        async with env.app_engine.begin() as conn:
            await conn.execute(sa.text(sql), params)

    def ago(**kw: float) -> datetime:
        return now - timedelta(**kw)

    for tag, age in (("old", ago(days=31)), ("new", ago(days=29))):
        await put(
            "INSERT INTO billing.processed_events (consumer, event_id, processed_at) "
            "VALUES ('hk', :e, :t)", e=tag, t=age,
        )  # fmt: skip
    for tag, age in (("old", ago(days=8)), ("new", ago(days=6))):
        await put(
            "INSERT INTO billing.outbox (topic, key, event_type, payload, created_at, published_at) "  # noqa: E501
            "VALUES ('t', :k, 'hk', '\\x00', :t, :t)", k=tag, t=age,
        )  # fmt: skip
    await put(  # published long ago but never published stays, however old
        "INSERT INTO billing.outbox (topic, key, event_type, payload, created_at) "
        "VALUES ('t', 'unpublished', 'hk', '\\x00', :t)", t=ago(days=90),
    )  # fmt: skip
    for tag, age in (("old", ago(hours=25)), ("new", ago(hours=23))):
        await put(
            "INSERT INTO billing.idempotency_keys (scope, key, request_hash, created_at) "
            "VALUES ('hk', :k, 'h', :t)", k=tag, t=age,
        )  # fmt: skip
    for tag, status, age in (
        ("old-done", "done", ago(days=15)),
        ("new-done", "done", ago(days=13)),
        ("old-dead", "dead", ago(days=15)),
        ("old-queued", "queued", ago(days=15)),
    ):
        await put(
            "INSERT INTO billing.jobs (kind, dedupe_key, run_at, status) VALUES ('hk', :k, :t, :s)",
            k=tag, t=age, s=status,
        )  # fmt: skip
    for tag, status, age in (
        ("old-processed", "processed", ago(days=181)),
        ("old-ignored", "ignored", ago(days=181)),
        ("new-processed", "processed", ago(days=179)),
        ("old-failed", "failed", ago(days=400)),
        ("old-received", "received", ago(days=400)),
    ):
        await put(
            "INSERT INTO billing.webhook_inbox (gateway, event_id, event_type, payload, status, received_at) "  # noqa: E501
            "VALUES ('hk', :e, 'x', '{}', :s, :t)", e=tag, s=status, t=age,
        )  # fmt: skip

    await env.run_job("housekeeping")
    await env.run_job("housekeeping")  # idempotent

    async def left(query: str) -> set[str]:
        return {r[0] for r in await env.sql(query)}

    assert await left("SELECT event_id FROM billing.processed_events WHERE consumer = 'hk'") == {
        "new"
    }
    assert await left("SELECT key FROM billing.outbox WHERE event_type = 'hk'") == {
        "new",
        "unpublished",
    }
    assert await left("SELECT key FROM billing.idempotency_keys WHERE scope = 'hk'") == {"new"}
    assert await left("SELECT dedupe_key FROM billing.jobs WHERE kind = 'hk'") == {
        "new-done", "old-dead", "old-queued",
    }  # fmt: skip
    assert await left("SELECT event_id FROM billing.webhook_inbox WHERE gateway = 'hk'") == {
        "new-processed", "old-failed", "old-received",
    }  # fmt: skip


# --- the 40-day timeline ---------------------------------------------------------------------


async def test_a_40_day_timeline_gives_the_expected_states_events_and_invoices(env: Env) -> None:
    """Signup, renewal, failed charge, grace, suspend, recovery, downgrade, cancel, end.

    Billing periods are 10 days (a fake-only period) so the whole life fits in 40 days."""
    env.set_period("10days")
    day0 = env.clock.now()  # 1 Nov 2026 10:15 IST

    def at(day: float) -> datetime:
        return day0 + timedelta(days=day)

    # D0 signup on Max
    options = await env.activate("max", MAX_MONTH)
    gid = options.gateway_subscription_id
    assert (await sub_row(env))[0] == "active" and await env.plan_code() == "max"

    # D9 the reminder for the renewal at D10; D10 renewal charge succeeds but its webhook is lost
    env.clock.set(at(9) - timedelta(hours=1))
    await env.run_job("renewal.remind")
    env.clock.set(at(10))
    env.gateway.charge(gid, MAX_MONTH)
    env.clock.set(at(10) + timedelta(hours=3))
    await env.run_job("subscription.poll")  # recovered by the poll
    assert await one(env, "SELECT count(*) FROM billing.payments") == 2

    # D19 reminder, D20 renewal fails -> past_due
    env.clock.set(at(19) - timedelta(hours=1))
    await env.run_job("renewal.remind")
    env.clock.set(at(20))
    await env.deliver(env.gateway.fail(gid))
    await env.process()
    assert (await sub_row(env))[0] == "past_due"
    assert (await sub_row(env))[1] == at(23)

    # D22 still inside the grace; D23 grace is over -> suspended, Free limits
    env.clock.set(at(22))
    await env.run_job("grace.expire")
    assert (await sub_row(env))[0] == "past_due"
    env.clock.set(at(23) + timedelta(minutes=5))
    await env.run_job("grace.expire")
    assert (await sub_row(env))[0] == "suspended" and await env.plan_code() == "free"

    # D24 Razorpay's retry succeeds -> active again, Max again
    env.clock.set(at(24))
    await env.deliver(env.gateway.charge(gid, MAX_MONTH))
    await env.process()
    assert (await sub_row(env))[0] == "active" and await env.plan_code() == "max"
    period_end = (await sub_row(env))[2]
    assert period_end == at(30)

    # D26 downgrade to Pro, authorised; the plan changes only at D30
    env.clock.set(at(26))
    down = await env.service.change_plan(env.user, "pro", Interval.MONTH)
    await env.deliver(env.gateway.authenticate(down.gateway_subscription_id))
    await env.process()
    await env.run_jobs()
    assert await env.plan_code() == "max"

    # D29 reminder names the Pro price; D30 the boundary charge on the new mandate
    env.clock.set(at(29) - timedelta(hours=1))
    await env.run_job("renewal.remind")
    env.clock.set(at(30))
    await env.deliver(env.gateway.charge(down.gateway_subscription_id, PRO_MONTH))
    await env.process()
    assert await env.plan_code() == "pro" and (await sub_row(env))[2] == at(40)

    # D32 the user cancels; D40 the period ends
    env.clock.set(at(32))
    await env.service.cancel(env.user)
    await env.run_jobs()
    assert (await sub_row(env))[0] == "cancelling" and await env.plan_code() == "pro"
    env.clock.set(at(39))
    await env.run_job("period.close")
    assert (await sub_row(env))[0] == "cancelling"
    env.clock.set(at(40) + timedelta(minutes=15))
    await env.run_job("period.close")
    assert (await sub_row(env))[0] == "ended" and await env.plan_code() == "free"

    # --- what the 40 days left behind -------------------------------------------------------
    await env.run_jobs()  # render the four invoices, which announces each with invoice.issued
    types = await event_types(env)
    assert types.count("invoice.issued") == 4  # one per invoice, none for the other events
    # entitlements.changed is announced once per state change: as many as there are history rows
    history = await one(env, "SELECT count(*) FROM billing.subscription_history")
    assert types.count("entitlements.changed") == history
    types = [t for t in types if t not in ("invoice.issued", "entitlements.changed")]
    assert types == [
        "subscription.changed",  # D0   incomplete -> active
        "subscription.renewal_upcoming",  # D9
        "subscription.renewal_upcoming",  # D19
        "payment.failed",  # D20
        "subscription.changed",  # D23  suspended
        "subscription.changed",  # D24  active
        "subscription.renewal_upcoming",  # D29  (Pro, the scheduled plan)
        "subscription.changed",  # D30  Max -> Pro
        "subscription.changed",  # D32  cancelling
        "subscription.changed",  # D40  ended
    ]
    changed = [
        (e["previous_status"], e["status"], e["plan_code"])
        for e in await event_bodies(env, "subscription.changed")
    ]  # noqa: E501
    assert changed == [
        ("incomplete", "active", "max"),
        ("past_due", "suspended", "max"),
        ("suspended", "active", "max"),
        ("active", "active", "pro"),
        ("active", "cancelling", "pro"),
        ("cancelling", "ended", "pro"),
    ]
    reminders = await event_bodies(env, "subscription.renewal_upcoming")
    assert [r["amount_paise"] for r in reminders] == [MAX_MONTH, MAX_MONTH, PRO_MONTH]

    invoices = await env.sql(
        "SELECT i.number, l.description, i.total_paise FROM billing.invoices i "
        "JOIN billing.invoice_lines l ON l.invoice_id = i.id ORDER BY i.number"
    )
    assert [(r[0], r[2]) for r in invoices] == [
        ("GA/2627/000001", MAX_MONTH),  # D0
        ("GA/2627/000002", MAX_MONTH),  # D10, from the poll
        ("GA/2627/000003", MAX_MONTH),  # D24, the late retry
        ("GA/2627/000004", PRO_MONTH),  # D30, the downgrade boundary
    ]
    assert [r[1].split(" · ")[0] for r in invoices] == [
        "GranthanAI Max", "GranthanAI Max", "GranthanAI Max", "GranthanAI Pro",
    ]  # fmt: skip
    history = await env.sql(
        "SELECT from_status::text, to_status::text FROM billing.subscription_history ORDER BY id"
    )
    states = [(r[0], r[1]) for r in history if r[0] != r[1]]
    assert states == [
        ("incomplete", "active"),
        ("active", "past_due"),
        ("past_due", "suspended"),
        ("suspended", "active"),
        ("active", "cancelling"),
        ("cancelling", "ended"),
    ]
    unbalanced = await env.sql(
        "SELECT transaction_id FROM billing.ledger_entries GROUP BY 1 HAVING sum(amount_paise) <> 0"
    )
    assert unbalanced == []
