"""Stage C: usage is rated and recorded exactly once, and the hold is settled."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey import keys
from billing.app.reserve import CheckAndReserveRequest
from billing.app.usage import UsageCall, UsageEvent, UsageService
from billing.domain.calendar import ist_day
from billing.domain.catalog import DenyReason, ModelGroup, OwnerType, ReserveKind
from billing.jobs.handlers.usage import usage_rollup
from billing.jobs.runner import JobContext
from billing.observability import USAGE_LATE_COMMIT
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import Stack, make_stack, make_user, msg
from tests.support.clock import TestClock

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 11:30 IST
ANSWER = UsageCall("anthropic", "claude-haiku-5-5", "answer", 6000, 0, 700)
HELPER = UsageCall("groq", "openai/gpt-oss-20b", "request_analysis", 2600, 0, 350)
FAST_ANSWER = UsageCall("groq", "openai/gpt-oss-20b", "answer", 1000, 0, 200)


def build(hot: FreshDb, vk: valkey_asyncio.Valkey) -> tuple[Stack, UsageService]:
    stack = make_stack(hot, vk, TestClock(NOW))
    service = UsageService(lambda: hot.app_uow(), stack.entitlements, stack.counters, stack.clock)
    return stack, service


def chat(
    uid: UUID,
    rid: str,
    hold: str | None,
    *,
    status: str = "success",
    group: str | None = "smart",
    calls: tuple[UsageCall, ...] = (ANSWER, HELPER),
) -> UsageEvent:
    return UsageEvent(
        kind=ReserveKind.MESSAGE,
        source_id=rid,
        dedupe_key=f"chat.response.generated:{rid}",
        user_id=uid,
        occurred_at=NOW,
        status=status,  # type: ignore[arg-type]
        hold_id=hold,
        model_group=group,
        model=calls[0].model,
        calls=calls,
    )


async def total(stack: Stack, uid: UUID) -> int:
    return (await stack.counters.day_counts(uid, ist_day(NOW))).get("total", 0)


async def rows(hot: FreshDb, uid: UUID) -> list[sa.Row[tuple[object, ...]]]:
    async with hot.app_uow() as uow:
        result = await uow.connection.execute(
            sa.text("SELECT * FROM billing.usage_events WHERE user_id = :u ORDER BY source_id"),
            {"u": uid},
        )
        return list(result)


async def reserve(stack: Stack, uid: UUID, group: ModelGroup = ModelGroup.SMART) -> str:
    r = await stack.reserve.check_and_reserve(msg(uid, group=group))
    assert r.allowed
    return r.hold_id


# --- exactly once ----------------------------------------------------------------------------


async def test_an_event_replayed_three_times_leaves_one_row_and_one_commit(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    hold = await reserve(stack, uid)
    results = [await usage.apply(chat(uid, "m1", hold)) for _ in range(3)]
    assert results == ["applied", "duplicate", "duplicate"]
    assert len(await rows(hot, uid)) == 1
    assert await total(stack, uid) == 1  # reserved once, committed once, never counted twice
    assert await stack.counters.peek_hold(uid, hold) is None
    async with hot.app_uow() as uow:
        claimed = await uow.connection.execute(
            sa.text("SELECT count(*) FROM billing.processed_events WHERE event_id = :k"),
            {"k": "chat.response.generated:m1"},
        )
        assert claimed.scalar_one() == 1


async def test_the_workbook_message_costs_118_750_micro_rupees(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    await usage.apply(chat(uid, "wb", await reserve(stack, uid)))
    (row,) = await rows(hot, uid)
    assert row.cost_micro_inr == 118_750
    assert (row.billable_units, row.price_paise) == (1, 0)  # covered by the plan
    assert (row.model, row.model_group) == ("claude-haiku-5-5", "smart")
    assert (row.input_tokens, row.output_tokens) == (6000, 700)
    assert (row.aux_input_tokens, row.aux_output_tokens) == (2600, 350)  # the helper call
    assert row.occurred_at == NOW  # from the event, never now()
    assert float(row.fx_usd_inr) == 95.0


async def test_a_late_event_after_the_hold_expired_is_counted_and_billed_to_the_plan(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    hold = await reserve(stack, uid)
    assert await total(stack, uid) == 1
    await stack.counters.release(uid, hold, "expire")  # the sweeper gave the message back
    assert await total(stack, uid) == 0

    late = USAGE_LATE_COMMIT._value.get()
    assert await usage.apply(chat(uid, "late", hold)) == "applied"
    assert await total(stack, uid) == 1  # counted after all (Decision 7)
    assert USAGE_LATE_COMMIT._value.get() == late + 1
    (row,) = await rows(hot, uid)
    async with hot.app_uow() as uow:
        pro = await uow.plans.current("pro", None)
    assert pro is not None and row.plan_version_id == pro.plan.id  # billed to the plan


async def test_an_event_without_a_hold_is_billed_to_the_plan_too(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "max")
    await usage.apply(chat(uid, "degraded", None, group="fast", calls=(FAST_ANSWER,)))
    (row,) = await rows(hot, uid)
    assert row.hold_id is None and row.billable_units == 1
    async with hot.app_uow() as uow:
        mx = await uow.plans.current("max", None)
    assert mx is not None and row.plan_version_id == mx.plan.id
    assert await total(stack, uid) == 1


# --- status rules ----------------------------------------------------------------------------


async def test_an_error_releases_the_hold_and_records_the_cost_at_price_zero(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    hold = await reserve(stack, uid)
    assert await total(stack, uid) == 1
    await usage.apply(chat(uid, "e1", hold, status="error", calls=(HELPER,)))
    assert await total(stack, uid) == 0  # given back
    assert await stack.counters.peek_hold(uid, hold) is None
    (row,) = await rows(hot, uid)
    assert row.billable_units == 0 and row.price_paise == 0
    assert row.cost_micro_inr > 0  # the helper call was paid for


async def test_cancelled_releases_without_output_and_commits_with_output(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    silent = UsageCall("anthropic", "claude-haiku-5-5", "answer", 6000, 0, 0)
    quiet_helper = UsageCall("groq", "openai/gpt-oss-20b", "request_analysis", 2600, 0, 0)
    h1 = await reserve(stack, uid)
    await usage.apply(chat(uid, "c1", h1, status="cancelled", calls=(silent, quiet_helper)))
    assert await total(stack, uid) == 0
    h2 = await reserve(stack, uid)
    await usage.apply(chat(uid, "c2", h2, status="cancelled", calls=(ANSWER, HELPER)))
    assert await total(stack, uid) == 1  # the user saw part of an answer
    by_id = {r.source_id: r for r in await rows(hot, uid)}
    assert by_id["c1"].billable_units == 0 and by_id["c2"].billable_units == 1


async def test_a_retried_answer_adds_to_cost_and_the_last_one_sets_the_units(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    first = UsageCall("anthropic", "claude-haiku-5-5", "answer", 6000, 0, 100)
    await usage.apply(chat(uid, "r1", await reserve(stack, uid), calls=(first, ANSWER)))
    (row,) = await rows(hot, uid)
    assert row.billable_units == 1 and row.output_tokens == 700  # the last answer
    assert row.aux_input_tokens == 6000 and row.aux_output_tokens == 100  # the earlier one
    # (6000*0.10 + 100*0.50) + (6000*0.10 + 700*0.50) = 1,600 USD-micro; x 95 = 0.152 INR
    assert row.cost_micro_inr == 152_000


async def test_an_unknown_purpose_is_cost_only(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    odd = UsageCall("groq", "openai/gpt-oss-20b", "title_generation", 100, 0, 10)
    await usage.apply(chat(uid, "o1", await reserve(stack, uid), calls=(ANSWER, odd)))
    (row,) = await rows(hot, uid)
    assert row.aux_input_tokens == 100 and row.billable_units == 1


# --- accounts --------------------------------------------------------------------------------


async def test_a_free_users_account_is_created_lazily_without_an_email(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot)  # Free, no account yet
    hold = await reserve(stack, uid, ModelGroup.FAST)
    await usage.apply(chat(uid, "f1", hold, group="fast", calls=(FAST_ANSWER,)))
    async with hot.app_uow() as uow:
        account = await uow.accounts.get_by_owner(OwnerType.USER, uid)
    assert account is not None and account.billing_email is None
    (row,) = await rows(hot, uid)
    assert row.billing_account_id == account.id


# --- manim and youtube -----------------------------------------------------------------------


async def test_a_manim_render_costs_the_calls_plus_the_render(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    r = await stack.reserve.check_and_reserve(
        CheckAndReserveRequest(uid, str(uuid4()), ReserveKind.MANIM_VIDEO)
    )
    assert r.allowed
    plan_call = UsageCall("groq", "openai/gpt-oss-20b", "manim_plan", 1000, 0, 500)
    event = UsageEvent(
        ReserveKind.MANIM_VIDEO, "job-1", "evt-manim-1", uid, NOW, "success", r.hold_id,
        calls=(plan_call,),
    )  # fmt: skip
    await usage.apply(event)
    (row,) = await rows(hot, uid)
    calls_cost = round((1000 * 0.075 + 500 * 0.30) / 1_000_000 * 95 * 1_000_000)  # 21,375
    assert row.kind == "manim_video" and row.billable_units == 1
    assert row.cost_micro_inr == calls_cost + 950_000  # + $0.01 at 95
    assert (await stack.counters.day_counts(uid, ist_day(NOW))).get("manim") == 1


async def test_the_fourth_free_youtube_video_is_denied_and_an_allowed_one_is_rated(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot)  # Free: 3 a month
    holds = []
    for _ in range(3):
        r = await stack.reserve.check_and_reserve(
            CheckAndReserveRequest(uid, str(uuid4()), ReserveKind.YOUTUBE_VIDEO)
        )
        assert r.allowed
        holds.append(r.hold_id)
    fourth = await stack.reserve.check_and_reserve(
        CheckAndReserveRequest(uid, str(uuid4()), ReserveKind.YOUTUBE_VIDEO)
    )
    assert not fourth.allowed and fourth.deny_reason is DenyReason.MONTHLY_LIMIT

    event = UsageEvent(ReserveKind.YOUTUBE_VIDEO, "yt-1", "evt-yt-1", uid, NOW, "success", holds[0])
    await usage.apply(event)
    (row,) = await rows(hot, uid)
    assert row.kind == "youtube_video" and row.billable_units == 1
    assert row.cost_micro_inr == 300_000  # INR 0.30
    month = await stack.counters.month_counts(uid, NOW)
    assert month.get("youtube") == 3


# --- thresholds ------------------------------------------------------------------------------


async def test_thresholds_fire_once_at_80_and_once_at_100(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot)  # Free: 25 messages a day
    for n in range(1, 26):
        hold = await reserve(stack, uid, ModelGroup.FAST)
        await usage.apply(chat(uid, f"t{n}", hold, group="fast", calls=(FAST_ANSWER,)))
    async with hot.app_uow() as uow:
        result = await uow.connection.execute(
            sa.text(
                "SELECT payload FROM billing.outbox WHERE event_type = 'usage.threshold_reached'"
            )
        )
        events = [
            json.loads(bytes(r.payload))
            for r in result
            if json.loads(bytes(r.payload))["user_id"] == str(uid)
        ]
    assert [(e["limit"], e["percent"], e["used"], e["allowed"]) for e in events] == [
        ("messages_per_day", 80, 20, 25),
        ("messages_per_day", 100, 25, 25),
    ]
    assert events[0]["resets_at"].startswith("2026-11-10T18:30:00")  # next IST midnight in UTC
    assert await vk.get(keys.threshold(uid, "messages_per_day", "20261110", 80)) == "1"


# --- the daily rollup ------------------------------------------------------------------------


async def snapshot(hot: FreshDb) -> list[tuple[object, ...]]:
    async with hot.app_uow() as uow:
        result = await uow.connection.execute(
            sa.text(
                "SELECT day, billing_account_id, user_id, model_group::text, messages, "
                "billable_units, cost_micro_inr, price_paise FROM billing.usage_daily "
                "ORDER BY day, user_id, model_group"
            )
        )
        return [tuple(r) for r in result]


async def test_replaying_a_recorded_day_twice_gives_identical_usage_daily(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    a, b = await make_user(hot, "pro"), await make_user(hot, "max")
    events: list[UsageEvent] = []
    for user, tag in ((a, "a"), (b, "b")):
        for n in range(3):
            hold = await reserve(stack, user)
            events.append(chat(user, f"{tag}{n}", hold))
    hold = await reserve(stack, a)
    events.append(chat(a, "a-fail", hold, status="error", calls=(HELPER,)))
    ctx = JobContext(lambda: hot.app_uow(), stack.clock, None)  # type: ignore[arg-type]

    for ev in events:
        await usage.apply(ev)
    await usage_rollup({"day": "2026-11-10"}, ctx)
    first = [r for r in await snapshot(hot) if r[2] in (a, b)]
    assert first and {r[4] for r in first if r[2] == a} == {3}  # the failed attempt is no message

    for ev in events:  # the whole day again
        assert await usage.apply(ev) == "duplicate"
    await usage_rollup({"day": "2026-11-10"}, ctx)
    await usage_rollup({"day": "2026-11-10"}, ctx)  # and the rollup itself twice
    assert [r for r in await snapshot(hot) if r[2] in (a, b)] == first
    # failed attempts still cost money, so their cost is in the aggregate
    a_row = next(r for r in first if r[2] == a)
    assert a_row[6] > 3 * 118_750 - 1


async def test_the_rollup_defaults_to_yesterday_in_ist(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack, usage = build(hot, vk)
    uid = await make_user(hot, "pro")
    await usage.apply(chat(uid, "y1", await reserve(stack, uid)))
    stack.clock.set(datetime(2026, 11, 11, 19, 0, tzinfo=UTC))  # 00:30 IST on the 12th
    ctx = JobContext(lambda: hot.app_uow(), stack.clock, None)  # type: ignore[arg-type]
    await usage_rollup({}, ctx)  # yesterday = the 11th: nothing happened that day
    assert [r for r in await snapshot(hot) if r[2] == uid] == []
    stack.clock.set(datetime(2026, 11, 10, 19, 0, tzinfo=UTC))  # 00:30 IST on the 11th
    await usage_rollup({}, ctx)  # yesterday = the 10th
    assert len([r for r in await snapshot(hot) if r[2] == uid]) == 1
