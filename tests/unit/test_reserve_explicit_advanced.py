"""L1: an explicitly requested Advanced ("best") model after the daily Advanced allowance is ALLOWED,
downgraded to Smart (limit.best_switched), and granted_model is a Smart model the plan includes (or empty),
never the Advanced model that was asked for. reserve.lua runs on the dev Valkey (database 14, unique users)."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey.counters import ValkeyCounters
from billing.app.reserve import CheckAndReserveRequest, ReserveService
from billing.domain.catalog import ModelGroup, ReserveKind
from billing.domain.entitlements import GrantSource

pytestmark = pytest.mark.asyncio
URL = os.environ.get("BILLING_RATE_TEST_VALKEY_URL", "redis://127.0.0.1:6380/14")
T0 = datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC)
OFFERED = [
    ("openai/gpt-oss-20b", ModelGroup.FAST),
    ("openai/gpt-oss-120b", ModelGroup.SMART),
    ("claude-haiku-5-5", ModelGroup.SMART),
    ("gemini-2.5-flash", ModelGroup.SMART),
    ("claude-sonnet-5-5", ModelGroup.BEST),
    ("gpt-6.1-sol", ModelGroup.BEST),
]


class Catalog:
    async def group_of(self, model):  # type: ignore[no-untyped-def]
        return dict((m, g) for m, g in OFFERED).get(model)

    async def offered(self):  # type: ignore[no-untyped-def]
        return list(OFFERED)

    async def models(self):  # type: ignore[no-untyped-def]
        return dict(OFFERED)


def entitlement(groups, best_per_day, extra=()):  # type: ignore[no-untyped-def]
    pv = SimpleNamespace(id=uuid4(), plan_code="pro", default_group=ModelGroup.SMART)
    limits = SimpleNamespace(
        messages_per_day=60, best_per_day=best_per_day, context_tokens=64000
    )
    return SimpleNamespace(
        grant=SimpleNamespace(plan_version=pv, billing_account_id=uuid4(), source=GrantSource.SUBSCRIPTION),
        limits=limits,
        allowed_groups=frozenset(groups),
        extra_models=frozenset(extra),
        account_blocked=False,
        overflow_wallet_account_id=None,
    )


@pytest.fixture
async def env():  # type: ignore[no-untyped-def]
    client = valkey_asyncio.from_url(URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip(f"no Valkey at {URL}")
    yield client
    async for k in client.scan_iter(match="*l1adv*"):
        await client.delete(k)
    await client.aclose()


def make(client, ent):  # type: ignore[no-untyped-def]
    class Ents:
        async def resolve(self, _u):  # type: ignore[no-untyped-def]
            return ent

    ids = iter(range(10_000))
    return ReserveService(
        Ents(),  # type: ignore[arg-type]
        Catalog(),  # type: ignore[arg-type]
        ValkeyCounters(client),
        SimpleNamespace(now=lambda: T0),  # type: ignore[arg-type]
        SimpleNamespace(new=lambda: f"l1adv-hold-{next(ids)}-{uuid4()}"),  # type: ignore[arg-type]
        max_messages_per_minute=-1,
    )


async def test_explicit_advanced_after_the_allowance_is_downgraded_to_a_plan_smart_model(env) -> None:  # type: ignore[no-untyped-def]
    ent = entitlement({ModelGroup.FAST, ModelGroup.SMART, ModelGroup.BEST}, 2)
    svc, u = make(env, ent), uuid4()
    for i in range(2):
        r = await svc.check_and_reserve(
            CheckAndReserveRequest(u, f"l1adv-{u}-{i}", ReserveKind.MESSAGE, requested_model="claude-sonnet-5-5")
        )
        assert r.allowed and r.granted_group is ModelGroup.BEST and not r.downgraded
        assert r.granted_model == "claude-sonnet-5-5"
    r = await svc.check_and_reserve(
        CheckAndReserveRequest(u, f"l1adv-{u}-3", ReserveKind.MESSAGE, requested_model="gpt-6.1-sol")
    )
    assert r.allowed and r.downgraded
    assert r.message_key == "limit.best_switched"
    assert r.granted_group is ModelGroup.SMART
    assert r.granted_model == "openai/gpt-oss-120b"  # a Smart model, never the Advanced one asked for
    assert r.granted_model not in ("gpt-6.1-sol", "claude-sonnet-5-5")


async def test_downgraded_model_is_one_the_plan_includes(env) -> None:  # type: ignore[no-untyped-def]
    """A plan that includes only some Smart models (extras) gets one of those."""
    ent = entitlement({ModelGroup.FAST, ModelGroup.BEST}, 0, extra={"claude-haiku-5-5"})
    svc, u = make(env, ent), uuid4()
    r = await svc.check_and_reserve(
        CheckAndReserveRequest(u, f"l1adv-{u}-1", ReserveKind.MESSAGE, requested_model="claude-sonnet-5-5")
    )
    assert r.allowed and r.downgraded and r.granted_group is ModelGroup.SMART
    assert r.granted_model == "claude-haiku-5-5"


async def test_allowed_models_use_the_offered_list_only(env) -> None:  # type: ignore[no-untyped-def]
    ent = entitlement({ModelGroup.FAST, ModelGroup.SMART, ModelGroup.BEST}, 5)
    svc = make(env, ent)
    assert await svc.allowed_models(ent) == sorted(m for m, _ in OFFERED)  # type: ignore[arg-type]
