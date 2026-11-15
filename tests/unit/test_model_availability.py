"""M2: billing keeps the models llm-service cannot answer, fails open without data, and refuses an explicit one."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from billing.app.model_availability import KEY, ModelAvailability
from billing.app.model_picker import ModelPicker
from billing.app.reserve import CheckAndReserveRequest, ReserveService, model_unavailable_message
from billing.app.catalog_spec import ModelEntry
from billing.domain.catalog import DenyReason, ModelGroup, ReserveKind
from billing.domain.entitlements import GrantSource
from billing.jobs.handlers import availability as job
from billing.observability import MODELS_UNAVAILABLE

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


class Kv:
    def __init__(self):
        self.d, self.ttl = {}, {}

    async def set(self, k, v, ex=None):
        self.d[k], self.ttl[k] = v, ex

    async def get(self, k):
        return self.d.get(k)


LLM = [
    {"id": "openai/gpt-oss-120b", "available": True, "reason": ""},
    {"id": "claude-haiku-5-5", "available": False, "reason": "no_credentials"},
    {"id": "gpt-6-luna", "available": False, "reason": "no_credentials"},
]


async def test_store_keeps_only_the_unavailable_with_a_15_minute_ttl():
    kv = Kv()
    av = ModelAvailability(kv)
    assert await av.store(LLM) == 2
    assert json.loads(kv.d[KEY]) == {"claude-haiku-5-5": "no_credentials", "gpt-6-luna": "no_credentials"}
    assert kv.ttl[KEY] == 900
    assert not await av.is_available("claude-haiku-5-5") and await av.is_available("openai/gpt-oss-120b")


async def test_no_data_fails_open():
    av = ModelAvailability(Kv())
    assert await av.unavailable() is None
    assert await av.is_available("claude-haiku-5-5")


async def test_valkey_trouble_fails_open():
    class Broken:
        async def get(self, k):
            raise ConnectionError("down")

    assert await ModelAvailability(Broken()).is_available("claude-haiku-5-5")


def reserve_service(av):
    ent = SimpleNamespace(
        grant=SimpleNamespace(plan_version=SimpleNamespace(plan_code="free", id=uuid4(), default_group=ModelGroup.FAST),
                              billing_account_id=uuid4(), source=GrantSource.FREE),
        limits=SimpleNamespace(messages_per_day=25, best_per_day=0, context_tokens=16000),
        allowed_groups=frozenset({ModelGroup.FAST}), extra_models=frozenset({"claude-haiku-5-5"}),
        account_blocked=False, overflow_wallet_account_id=None)

    class Ents:
        async def resolve(self, _u):
            return ent

    return ReserveService(Ents(), None, None, SimpleNamespace(now=lambda: NOW), None, max_messages_per_minute=-1, availability=av)


async def test_explicit_unavailable_model_is_refused_with_model_unavailable():
    kv = Kv()
    av = ModelAvailability(kv)
    await av.store(LLM)
    r = await reserve_service(av).check_and_reserve(
        CheckAndReserveRequest(uuid4(), "k1", ReserveKind.MESSAGE, requested_model="claude-haiku-5-5"))
    assert not r.allowed and r.deny_reason is DenyReason.MODEL_NOT_IN_PLAN
    assert r.message_key == "model.unavailable"
    assert model_unavailable_message("Claude Haiku 5.5") == "Claude Haiku 5.5 isn't available right now. Please pick another model."


async def picker(av):
    class Uow:
        plans = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def models():
        return [ModelEntry("openai/gpt-oss-120b", "GPT-OSS 120B", "groq", ModelGroup.SMART, 0),
                ModelEntry("claude-haiku-5-5", "Claude Haiku 5.5", "anthropic", ModelGroup.SMART, 1)]

    async def none():
        return []

    async def cur(*a):
        return None

    Uow.plans.list_models, Uow.plans.all_current, Uow.plans.current = models, none, cur
    ent = SimpleNamespace(grant=SimpleNamespace(plan_version=SimpleNamespace(plan_code="free", default_group=ModelGroup.FAST), source=GrantSource.FREE),
                          limits=SimpleNamespace(messages_per_day=25, best_per_day=0), allowed_groups=frozenset({ModelGroup.FAST}),
                          extra_models=frozenset({"openai/gpt-oss-120b", "claude-haiku-5-5"}), overflow_wallet_account_id=None)

    class Ents:
        async def resolve(self, _u):
            return ent

    class Counters:
        async def day_counts(self, *a):
            return {}

    return ModelPicker(Uow, Ents(), Counters(), SimpleNamespace(now=lambda: NOW), availability=av)


async def test_catalog_and_me_models_mark_available_and_fail_open():
    av = ModelAvailability(Kv())
    p = await picker(av)
    assert all(m["available"] for m in await p.all_models())  # no data
    await av.store(LLM)
    assert {m["id"]: m["available"] for m in await (await picker(av)).all_models()} == {"openai/gpt-oss-120b": True, "claude-haiku-5-5": False}
    mine = await (await picker(av)).for_user(uuid4())
    smart = next(g for g in mine["groups"] if g["group"] == "smart")
    assert {m["id"]: (m["allowed"], m["available"]) for m in smart["models"]} == {"openai/gpt-oss-120b": (True, True), "claude-haiku-5-5": (True, False)}


async def test_job_stores_llm_answer_sets_the_gauge_and_keeps_old_data_when_llm_is_down(monkeypatch):
    kv = Kv()
    ctx = SimpleNamespace(availability=ModelAvailability(kv))
    job.URL.update(base="http://llm", token="t")
    seen = {}

    async def fake(base, token, timeout_s=5.0):
        seen.update(base=base, token=token)
        return LLM

    monkeypatch.setattr(job, "fetch_availability", fake)
    await job.refresh_availability({}, ctx)
    assert seen == {"base": "http://llm", "token": "t"} and MODELS_UNAVAILABLE._value.get() == 2

    async def down(*a, **k):
        raise ConnectionError("llm down")

    monkeypatch.setattr(job, "fetch_availability", down)
    await job.refresh_availability({}, ctx)
    assert json.loads(kv.d[KEY])  # the old set stays until it expires on its own
    job.URL.update(base="", token="")
