"""B4b: 30 messages a minute, every plan. The Lua script runs on the dev Valkey (database 14, unique users, cleaned up);
the reserve wrapper runs against an in-memory counter that has the same semantics."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey.counters import ValkeyCounters
from billing.app.reserve import CheckAndReserveRequest, CheckAndReserveResult, ReserveService
from billing.domain.catalog import DenyReason, ModelGroup, ReserveKind

pytestmark = pytest.mark.asyncio
URL = os.environ.get("BILLING_RATE_TEST_VALKEY_URL", "redis://127.0.0.1:6380/14")
T0 = datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC)


@pytest.fixture
async def counters():  # type: ignore[no-untyped-def]
    client = valkey_asyncio.from_url(URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip(f"no Valkey at {URL}")
    users: list = []
    c = ValkeyCounters(client)
    c.users = users  # type: ignore[attr-defined]
    yield c
    for u in users:
        async for k in client.scan_iter(match=f"rl:{{{u}}}:*"):
            await client.delete(k)
    await client.aclose()


async def test_the_31st_message_in_a_minute_is_denied_and_the_next_minute_is_allowed(
    counters,
) -> None:  # type: ignore[no-untyped-def]
    u = uuid4()
    counters.users.append(u)
    results = [await counters.rate_check(u, f"m{i}", T0, 30) for i in range(31)]
    assert results == [True] * 30 + [False]
    assert (
        await counters.rate_check(u, "m32", T0 + timedelta(seconds=30), 30) is False
    )  # same minute
    assert await counters.rate_check(u, "m33", T0 + timedelta(minutes=1), 30) is True  # next minute


async def test_a_retry_of_a_counted_request_is_not_counted_twice_and_a_denied_one_is_not_remembered(
    counters,
) -> None:  # type: ignore[no-untyped-def]
    u = uuid4()
    counters.users.append(u)
    for i in range(30):
        assert await counters.rate_check(u, f"m{i}", T0, 30)
    assert await counters.rate_check(u, "m0", T0, 30) is True  # a replay of an allowed request
    assert await counters.rate_check(u, "extra", T0, 30) is False
    assert (
        await counters.rate_check(u, "extra", T0, 30) is False
    )  # still denied: the denial left no marker
    assert await counters.rate_check(u, "m5", T0, -1) is True  # -1 = off


async def test_giving_the_minute_back_frees_a_slot(counters) -> None:  # type: ignore[no-untyped-def]
    u = uuid4()
    counters.users.append(u)
    for i in range(30):
        assert await counters.rate_check(u, f"m{i}", T0, 30)
    await counters.rate_release(u, "m3", T0)
    assert await counters.rate_check(u, "new", T0, 30) is True
    await counters.rate_release(
        u, "never-counted", T0
    )  # a release of something not counted changes nothing
    assert await counters.rate_check(u, "another", T0, 30) is False


class MemCounters:
    """The same rules as rate.lua, in memory, plus a record of whether the daily reserve was reached."""

    def __init__(self) -> None:
        self.n: dict[tuple, int] = {}
        self.seen: set[tuple] = set()
        self.reserve_calls = 0

    async def rate_check(self, user, key, now, limit):  # type: ignore[no-untyped-def]
        m = (user, now.strftime("%Y%m%d%H%M"))
        if limit < 0 or (user, key) in self.seen:
            return True
        if self.n.get(m, 0) + 1 > limit:
            return False
        self.n[m] = self.n.get(m, 0) + 1
        self.seen.add((user, key))
        return True

    async def rate_release(self, user, key, now):  # type: ignore[no-untyped-def]
        if (user, key) in self.seen:
            self.seen.discard((user, key))
            self.n[(user, now.strftime("%Y%m%d%H%M"))] -= 1


class Svc(ReserveService):
    """Only the per-minute wrapper under test; the plan decision is scripted."""

    plan_result: CheckAndReserveResult

    async def _decide_plan(self, req, ent):  # type: ignore[no-untyped-def]
        self._counters.reserve_calls += 1  # type: ignore[attr-defined]
        return self.plan_result


def service(limit: int, plan_result: CheckAndReserveResult):  # type: ignore[no-untyped-def]
    clock = SimpleNamespace(now=lambda: T0)
    mem = MemCounters()
    svc = Svc(None, None, mem, clock, None, max_messages_per_minute=limit)  # type: ignore[arg-type]
    svc.plan_result = plan_result
    return svc, mem


ALLOWED = CheckAndReserveResult(allowed=True, hold_id="h")
DAILY = CheckAndReserveResult(allowed=False, deny_reason=DenyReason.DAILY_LIMIT)
ENT = SimpleNamespace(account_blocked=False, grant=SimpleNamespace(billing_account_id=None))


def req(user, key, kind=ReserveKind.MESSAGE):  # type: ignore[no-untyped-def]
    return CheckAndReserveRequest(user, key, kind, ModelGroup.FAST)


async def test_a_rate_limited_message_never_reaches_the_daily_counter_or_a_hold() -> None:
    svc, mem = service(2, ALLOWED)
    u = uuid4()
    assert (await svc._decide(req(u, "a"), ENT)).allowed  # type: ignore[arg-type]
    assert (await svc._decide(req(u, "b"), ENT)).allowed  # type: ignore[arg-type]
    before = mem.reserve_calls
    third = await svc._decide(req(u, "c"), ENT)  # type: ignore[arg-type]
    assert not third.allowed and third.deny_reason is DenyReason.RATE_LIMIT
    assert third.message_key == "limit.per_minute"
    assert mem.reserve_calls == before  # the daily reserve (and so any hold) was never attempted


async def test_a_message_refused_for_another_reason_gives_its_minute_back() -> None:
    svc, mem = service(1, DAILY)
    u = uuid4()
    for i in range(5):  # every attempt is denied by the daily limit, none may eat the single slot
        r = await svc._decide(req(u, f"k{i}"), ENT)  # type: ignore[arg-type]
        assert r.deny_reason is DenyReason.DAILY_LIMIT
    svc.plan_result = ALLOWED
    assert (await svc._decide(req(u, "ok"), ENT)).allowed  # type: ignore[arg-type]


async def test_only_messages_are_rate_limited_and_the_limit_can_be_switched_off() -> None:
    svc, mem = service(1, ALLOWED)
    u = uuid4()
    for i in range(4):  # videos do not count
        assert (await svc._decide(req(u, f"v{i}", ReserveKind.YOUTUBE_VIDEO), ENT)).allowed  # type: ignore[arg-type]
    off, _ = service(-1, ALLOWED)
    for i in range(50):
        assert (await off._decide(req(u, f"m{i}"), ENT)).allowed  # type: ignore[arg-type]


async def test_the_catalog_default_is_thirty() -> None:
    from pathlib import Path

    from billing.adapters.catalog.yaml_loader import load_catalog

    spec = load_catalog(Path(__file__).resolve().parents[2] / "config" / "plans.yaml")
    assert spec.payg.max_messages_per_minute == 30
