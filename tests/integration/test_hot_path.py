import asyncio
from datetime import UTC, datetime

import pytest
import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey import keys
from billing.app.records import UsageEventRecord
from billing.cli import _valkey_rebuild
from billing.domain.calendar import ist_day
from billing.domain.catalog import DenyReason, ModelGroup, OwnerType, ReserveKind
from billing.domain.money import MicroInr, Paise
from billing.jobs.handlers.holds import sweep_expired_holds
from billing.jobs.runner import JobContext
from billing.observability import HOLDS_EXPIRED
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import grant_plan, make_stack, make_user, msg
from tests.support.clock import TestClock

NOV = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


async def total(stack, uid, now=None) -> int:  # type: ignore[no-untyped-def]
    counts = await stack.counters.day_counts(uid, ist_day(now or stack.clock.now()))
    return counts.get("total", 0)


# --- concurrency -----------------------------------------------------------------------------


async def test_200_concurrent_reserves_allow_exactly_the_limit(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot, "pro")
    results = await asyncio.gather(*(stack.reserve.check_and_reserve(msg(uid)) for _ in range(200)))
    allowed = [r for r in results if r.allowed]
    denied = [r for r in results if not r.allowed]
    assert len(allowed) == 60 and len(denied) == 140
    assert {r.deny_reason for r in denied} == {DenyReason.DAILY_LIMIT}
    assert {r.message_key for r in denied} == {"limit.daily"}
    assert len({r.hold_id for r in allowed}) == 60
    assert await total(stack, uid) == 60
    assert sorted(r.remaining_today for r in allowed)[:3] == [0, 1, 2]


async def test_two_tabs_racing_at_59_of_60_only_one_wins(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot, "pro")
    for _ in range(59):
        assert (await stack.reserve.check_and_reserve(msg(uid))).allowed
    a, b = await asyncio.gather(
        stack.reserve.check_and_reserve(msg(uid)), stack.reserve.check_and_reserve(msg(uid))
    )
    assert sorted([a.allowed, b.allowed]) == [False, True]


# --- behaviour -------------------------------------------------------------------------------


async def test_idempotent_retry_and_retry_after_upgrade(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot)  # Free: 25 a day
    first = await stack.reserve.check_and_reserve(msg(uid, "same-key"))
    again = await stack.reserve.check_and_reserve(msg(uid, "same-key"))
    assert first.allowed and again.allowed and first.hold_id == again.hold_id
    assert await total(stack, uid) == 1  # counted once

    for i in range(24):
        assert (await stack.reserve.check_and_reserve(msg(uid, f"k{i}"))).allowed
    denied = await stack.reserve.check_and_reserve(msg(uid, "k-over"))
    assert denied.deny_reason is DenyReason.DAILY_LIMIT and denied.hold_id == ""

    await grant_plan(hot, uid, "pro")  # upgrade, then the cache is invalidated
    await stack.entitlements.invalidate(uid)
    retry = await stack.reserve.check_and_reserve(msg(uid, "k-over"))  # same key as the deny
    assert retry.allowed and retry.max_context_tokens == 64000


async def test_max_users_13th_best_is_served_as_smart(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot, "max")
    for _ in range(12):
        r = await stack.reserve.check_and_reserve(msg(uid, group=ModelGroup.BEST))
        assert r.allowed and r.granted_group is ModelGroup.BEST and not r.downgraded
    r13 = await stack.reserve.check_and_reserve(msg(uid, group=ModelGroup.BEST))
    assert r13.allowed and r13.granted_group is ModelGroup.SMART and r13.downgraded
    assert r13.message_key == "limit.best_switched"
    assert r13.max_context_tokens == 128000 and r13.funded_by == "plan"
    counts = await stack.counters.day_counts(uid, ist_day(stack.clock.now()))
    assert (counts["best"], counts["smart"], counts["total"]) == (12, 1, 13)
    assert r13.remaining_today == 80 - 13


async def test_free_plan_models(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot)
    extra = await stack.reserve.check_and_reserve(msg(uid, group=None, model="openai/gpt-oss-120b"))
    assert extra.allowed and extra.granted_group is ModelGroup.SMART  # counted as smart
    best = await stack.reserve.check_and_reserve(msg(uid, group=ModelGroup.BEST))
    assert best.deny_reason is DenyReason.MODEL_NOT_IN_PLAN
    assert best.message_key == "plan.model_not_included"
    unknown = await stack.reserve.check_and_reserve(msg(uid, group=None, model="no-such-model"))
    assert unknown.deny_reason is DenyReason.MODEL_NOT_IN_PLAN
    smart_model = await stack.reserve.check_and_reserve(msg(uid, group=None, model="gpt-6-luna"))
    assert smart_model.deny_reason is DenyReason.MODEL_NOT_IN_PLAN  # smart group, not extra
    with pytest.raises(ValueError):
        await stack.reserve.check_and_reserve(msg(uid, group=None))
    assert await total(stack, uid) == 1  # denies never touch the counters


async def test_youtube_limit_is_monthly_in_ist(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    clock = TestClock(NOV)
    stack = make_stack(hot, vk, clock)
    uid = await make_user(hot)  # Free: 3 a month
    yt = {"kind": ReserveKind.YOUTUBE_VIDEO, "group": None}
    for _ in range(3):
        assert (await stack.reserve.check_and_reserve(msg(uid, **yt))).allowed
    fourth = await stack.reserve.check_and_reserve(msg(uid, **yt))
    assert fourth.deny_reason is DenyReason.MONTHLY_LIMIT and fourth.message_key == "limit.monthly"
    clock.set(datetime(2026, 11, 30, 18, 30, tzinfo=UTC))  # 1 Dec 00:00 IST
    assert (await stack.reserve.check_and_reserve(msg(uid, **yt))).allowed
    manim = await stack.reserve.check_and_reserve(
        msg(uid, kind=ReserveKind.MANIM_VIDEO, group=None)
    )
    assert manim.allowed and manim.remaining_today == -1 and manim.granted_group is None


async def test_ist_midnight_resets_the_daily_limit(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    clock = TestClock(datetime(2026, 11, 1, 18, 29, 59, tzinfo=UTC))
    stack = make_stack(hot, vk, clock)
    uid = await make_user(hot)  # Free: 25 a day
    for _ in range(25):
        assert (await stack.reserve.check_and_reserve(msg(uid))).allowed
    assert not (await stack.reserve.check_and_reserve(msg(uid))).allowed  # at the limit
    old_key = keys.day_counters(uid, clock.now())
    assert old_key.endswith("20261101")

    clock.set(datetime(2026, 11, 1, 18, 30, 0, tzinfo=UTC))  # midnight IST
    assert (await stack.reserve.check_and_reserve(msg(uid))).allowed
    assert keys.day_counters(uid, clock.now()).endswith("20261102")
    assert 0 < await vk.ttl(old_key) <= 172_800  # yesterday's counters expire on their own


async def test_release_gives_the_message_back_once(hot: FreshDb, vk: valkey_asyncio.Valkey) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot)
    r = await stack.reserve.check_and_reserve(msg(uid))
    assert await total(stack, uid) == 1
    assert await stack.reserve.release(uid, r.hold_id, "provider_error") is True
    assert await total(stack, uid) == 0
    assert await stack.reserve.release(uid, r.hold_id, "provider_error") is False
    assert await total(stack, uid) == 0  # never negative


async def test_expired_hold_is_swept_and_late_usage_counts_again(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    await vk.delete(
        keys.HOLDS_DUE
    )  # holds other tests left behind would be swept (and counted) too
    stack = make_stack(hot, vk)
    uid = await make_user(hot)
    r = await stack.reserve.check_and_reserve(msg(uid))
    assert await total(stack, uid) == 1
    await vk.zadd(keys.HOLDS_DUE, {keys.due_member(uid, r.hold_id): 1})  # long overdue

    before = HOLDS_EXPIRED.labels(mode="plan")._value.get()  # type: ignore[attr-defined]
    ctx = JobContext(lambda: hot.app_uow(), stack.clock, None, counters=stack.counters)  # type: ignore[arg-type]
    await sweep_expired_holds({}, ctx)
    assert await total(stack, uid) == 0  # the message was given back (Decision 7)
    assert HOLDS_EXPIRED.labels(mode="plan")._value.get() == before + 1  # type: ignore[attr-defined]
    assert await vk.zscore(keys.HOLDS_DUE, keys.due_member(uid, r.hold_id)) is None

    now = stack.clock.now()
    args = (uid, r.hold_id, Paise(0), "src-1", ReserveKind.MESSAGE, "fast", now)
    assert await stack.counters.commit(*args) == "OK"  # the answer arrived after all
    assert await total(stack, uid) == 1
    assert await stack.counters.commit(*args) == "DUP"  # committing twice is a no-op
    assert await total(stack, uid) == 1


async def test_committing_a_live_hold_keeps_the_count(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot)
    r = await stack.reserve.check_and_reserve(msg(uid))
    peek = await stack.counters.peek_hold(uid, r.hold_id)
    assert peek is not None and peek.group is ModelGroup.FAST and peek.mode == "plan"
    assert peek.expires_at is not None and peek.billing_account_id is None
    now = stack.clock.now()
    assert (
        await stack.counters.commit(uid, r.hold_id, Paise(0), "s", ReserveKind.MESSAGE, "fast", now)
        == "OK"
    )
    assert await total(stack, uid) == 1  # unchanged: it was already counted
    assert await stack.counters.peek_hold(uid, r.hold_id) is None
    assert await stack.reserve.release(uid, r.hold_id, "x") is False  # committed holds are gone


async def test_entitlement_cache_and_blocked_account(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot, "pro")
    await stack.entitlements.resolve(uid)
    first = len(stack.uow_calls)
    assert first >= 1
    await stack.entitlements.resolve(uid)
    assert len(stack.uow_calls) == first  # a cache hit does not open a Postgres transaction
    await stack.entitlements.invalidate(uid)
    await stack.entitlements.resolve(uid)
    assert len(stack.uow_calls) == first + 1

    blocked = await make_user(hot, "pro", blocked=True)
    r = await stack.reserve.check_and_reserve(msg(blocked))
    assert not r.allowed and r.deny_reason is DenyReason.ACCOUNT_BLOCKED
    assert r.message_key == "account.blocked"

    ent = await stack.reserve.get_entitlements(uid)
    assert ent.grant.plan_version.plan_code == "pro" and not ent.account_blocked
    assert "claude-haiku-5-5" in await stack.reserve.allowed_models(ent)
    assert "claude-opus-5-5" not in await stack.reserve.allowed_models(ent)


# --- rebuild ---------------------------------------------------------------------------------


async def test_rebuild_reproduces_the_counters(
    hot: FreshDb, vk: valkey_asyncio.Valkey, monkeypatch: pytest.MonkeyPatch, valkey_url: str
) -> None:
    stack = make_stack(hot, vk)
    uid = await make_user(hot, "pro")
    async with hot.app_uow() as uow:
        account = await uow.accounts.get_or_create(OwnerType.USER, uid, "r@example.com")
    plan = (await stack.entitlements.resolve(uid)).grant.plan_version

    plan_mix = [("fast", ReserveKind.MESSAGE)] * 3 + [("smart", ReserveKind.MESSAGE)] * 2
    plan_mix += [("youtube", ReserveKind.YOUTUBE_VIDEO)] * 2 + [("manim", ReserveKind.MANIM_VIDEO)]
    for n, (field, kind) in enumerate(plan_mix):
        group = ModelGroup(field) if kind is ReserveKind.MESSAGE else None
        r = await stack.reserve.check_and_reserve(msg(uid, group=group, kind=kind))
        assert r.allowed
        now = stack.clock.now()
        await stack.counters.commit(uid, r.hold_id, Paise(0), f"s{n}", kind, field, now)
        async with hot.app_uow() as uow:
            await uow.usage.insert(
                UsageEventRecord(
                    kind={
                        ReserveKind.MESSAGE: "message",
                        ReserveKind.YOUTUBE_VIDEO: "youtube_video",
                        ReserveKind.MANIM_VIDEO: "manim_video",
                    }[kind],
                    source_id=f"s{n}-{uid}",
                    occurred_at=now,
                    user_id=uid,
                    billing_account_id=account.id,
                    plan_version_id=plan.id,
                    billable_units=1,
                    cost_micro_inr=MicroInr(100),
                    fx_usd_inr="95.0000",
                    model_group=ModelGroup(field) if kind is ReserveKind.MESSAGE else None,
                )
            )
    day = ist_day(stack.clock.now())
    before_day = await stack.counters.day_counts(uid, day)
    month_key = keys.month_counters(uid, stack.clock.now())
    before_month = await vk.hgetall(month_key)
    assert before_day == {"fast": 3, "smart": 2, "manim": 1, "total": 6}
    assert before_month == {"youtube": "2", "total": "2"}

    await vk.flushall()
    assert await stack.counters.day_counts(uid, day) == {}
    monkeypatch.setenv("BILLING_DATABASE_URL", hot.app_url)
    monkeypatch.setenv("BILLING_VALKEY_URL", valkey_url)
    assert await _valkey_rebuild(day) == 0

    assert await stack.counters.day_counts(uid, day) == before_day
    assert await vk.hgetall(month_key) == before_month
    assert 0 < await vk.ttl(keys.day_counters_for(uid, day)) <= 172_800


async def test_invalidation_on_one_replica_reaches_the_other_within_100_ms(
    hot: FreshDb, vk: valkey_asyncio.Valkey
) -> None:
    import asyncio
    import time

    from billing.adapters.valkey.entitlement_cache import ValkeyEntitlementCache
    from billing.app.entitlements import EntitlementService

    clock = make_stack(hot, vk).clock
    uid = await make_user(hot)

    def replica() -> EntitlementService:
        return EntitlementService(
            lambda: hot.app_uow(), ValkeyEntitlementCache(vk), clock, l1_ttl_seconds=60.0
        )

    a, b = replica(), replica()
    ready = asyncio.Event()
    listener = asyncio.create_task(b.listen(ready))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        first = await b.resolve(uid)  # b now holds it in L1 (60 s: only the message can clear it)
        assert uid in b._l1
        await a.invalidate(uid)
        started = time.monotonic()
        while uid in b._l1 and time.monotonic() - started < 1:
            await asyncio.sleep(0.005)
        assert uid not in b._l1
        assert time.monotonic() - started < 0.1
        assert (await b.resolve(uid)).version_hash == first.version_hash
    finally:
        listener.cancel()
