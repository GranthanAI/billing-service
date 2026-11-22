"""L1: /me/models per plan: allowed models exactly as the plan lineup, and the Advanced allowance."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from billing.app.catalog_spec import ModelEntry
from billing.app.model_picker import ModelPicker
from billing.domain.catalog import ModelGroup as G
from billing.domain.entitlements import GrantSource

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
CATALOG = [
    ("openai/gpt-oss-20b", G.FAST), ("gemini-3.1-flash-lite", G.FAST),
    ("openai/gpt-oss-120b", G.SMART), ("claude-haiku-5-5", G.SMART), ("gpt-6-luna", G.SMART),
    ("gemini-3.5-flash-lite", G.SMART), ("gemini-3.8-flash", G.SMART),
    ("claude-sonnet-5-5", G.BEST), ("gpt-6.1-sol", G.BEST),
    ("claude-opus-5-5", G.ULTRA),
]  # fmt: skip
FAST = {"openai/gpt-oss-20b", "gemini-3.1-flash-lite"}
SMART = {"openai/gpt-oss-120b", "claude-haiku-5-5", "gpt-6-luna", "gemini-3.5-flash-lite", "gemini-3.8-flash"}
BEST = {"claude-sonnet-5-5", "gpt-6.1-sol"}


class Uow:
    def __init__(self) -> None:
        self.plans = SimpleNamespace(list_models=self._models, all_current=self._none, current=self._cur)

    async def _models(self):  # type: ignore[no-untyped-def]
        return [ModelEntry(m, m, "x", g, i) for i, (m, g) in enumerate(CATALOG)]

    async def _none(self):  # type: ignore[no-untyped-def]
        return []

    async def _cur(self, *_a):  # type: ignore[no-untyped-def]
        return None

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


def picker(groups, extra, msgs, best, used_best=0):  # type: ignore[no-untyped-def]
    ent = SimpleNamespace(
        grant=SimpleNamespace(
            plan_version=SimpleNamespace(plan_code="x", default_group=G.SMART), source=GrantSource.FREE
        ),
        limits=SimpleNamespace(messages_per_day=msgs, best_per_day=best),
        allowed_groups=frozenset(groups),
        extra_models=frozenset(extra),
        overflow_wallet_account_id=None,
    )

    class Ents:
        async def resolve(self, _u):  # type: ignore[no-untyped-def]
            return ent

    class Counters:
        async def day_counts(self, *_a):  # type: ignore[no-untyped-def]
            return {"total": used_best, "best": used_best}

    return ModelPicker(Uow, Ents(), Counters(), SimpleNamespace(now=lambda: NOW))  # type: ignore[arg-type]


def allowed(out):  # type: ignore[no-untyped-def]
    return {m["id"] for g in out["groups"] for m in g["models"] if m["allowed"]}


async def test_free_includes_fast_plus_three_smart_models_and_no_advanced() -> None:
    out = await picker({G.FAST}, {"openai/gpt-oss-120b", "claude-haiku-5-5", "gpt-6-luna"}, 25, 0).for_user(None)  # type: ignore[arg-type]
    assert allowed(out) == FAST | {"openai/gpt-oss-120b", "claude-haiku-5-5", "gpt-6-luna"}
    best = next(g for g in out["groups"] if g["group"] == "best")
    assert best["allowed"] is False and best["daily_limit"] == 0
    smart = next(g for g in out["groups"] if g["group"] == "smart")
    assert smart["allowed"] is True and smart["remaining_today"] == 25  # shares the daily 25


@pytest.mark.parametrize(("limit", "used", "left"), [(5, 2, 3), (15, 15, 0)])
async def test_pro_and_max_include_advanced_with_a_daily_allowance(limit, used, left) -> None:  # type: ignore[no-untyped-def]
    out = await picker({G.FAST, G.SMART, G.BEST}, set(), 60, limit, used).for_user(None)  # type: ignore[arg-type]
    assert allowed(out) == FAST | SMART | BEST  # Ultra is credits-only
    best = next(g for g in out["groups"] if g["group"] == "best")
    assert (best["daily_limit"], best["remaining_today"]) == (limit, left)


async def test_me_models_returns_the_plans_default_model_only_when_it_is_usable() -> None:
    p = picker({G.FAST}, {"openai/gpt-oss-120b", "claude-haiku-5-5", "gpt-6-luna"}, 25, 0)
    p._ent  # noqa: B018
    ent = await p._ent.resolve(None)
    ent.grant.plan_version.default_model = "openai/gpt-oss-120b"
    out = await p.for_user(None)  # type: ignore[arg-type]
    assert out["default_model"] == "openai/gpt-oss-120b"
    ent.grant.plan_version.default_model = "claude-sonnet-5-5"          # not in the plan: no preference
    assert (await p.for_user(None))["default_model"] is None  # type: ignore[arg-type]
    ent.grant.plan_version.default_model = None
    assert (await p.for_user(None))["default_model"] is None  # type: ignore[arg-type]


async def test_every_model_carries_the_think_flag_in_both_lists() -> None:
    class ThinkUow(Uow):
        async def _models(self):  # type: ignore[no-untyped-def]
            return [ModelEntry(m, m, "x", g, i, think=(m != "gpt-6-luna")) for i, (m, g) in enumerate(CATALOG)]

    p = picker({G.FAST, G.SMART}, set(), 60, 0)
    p._uow = ThinkUow  # type: ignore[assignment]
    mine = await p.for_user(None)  # type: ignore[arg-type]
    flags = {m["id"]: m["think"] for g in mine["groups"] for m in g["models"]}
    assert flags["gpt-6-luna"] is False and flags["openai/gpt-oss-120b"] is True and len(flags) == len(CATALOG)
    everyone = await p.all_models()
    assert {m["id"]: m["think"] for m in everyone} == flags
