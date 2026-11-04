"""GET /billing/v1/models and /billing/v1/me/models."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest_asyncio
import valkey.asyncio as valkey_asyncio

from billing.app.model_picker import ModelPicker
from billing.domain.catalog import ModelGroup
from billing.main_api import app
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import Stack, make_stack, make_user, msg


def client_for(stack: Stack, hot: FreshDb, *, payg: bool = False) -> httpx.AsyncClient:
    app.state.picker = ModelPicker(
        lambda: hot.app_uow(),
        stack.entitlements,
        stack.counters,
        stack.clock,
        payg_enabled=payg,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def stack(hot: FreshDb, vk: valkey_asyncio.Valkey) -> AsyncIterator[Stack]:
    yield make_stack(hot, vk)


def group(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(g for g in body["groups"] if g["group"] == name)


async def test_models_list_etag_and_304(stack: Stack, hot: FreshDb) -> None:
    async with client_for(stack, hot) as http:
        r = await http.get("/billing/v1/models")  # public: no header
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=300"
        models = r.json()["models"]
        assert len(models) == 12
        assert models[0] == {
            "id": "openai/gpt-oss-20b",
            "label": "GPT-OSS 20B",
            "provider": "groq",
            "group": "fast",
            "sort_order": 0,
        }
        assert [m["sort_order"] for m in models] == list(range(12))
        assert {m["group"] for m in models} == {"fast", "smart", "best", "ultra"}
        etag = r.headers["etag"]
        again = await http.get("/billing/v1/models", headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        other = await http.get("/billing/v1/models", headers={"If-None-Match": '"nope"'})
        assert other.status_code == 200


async def test_me_models_for_a_free_user(stack: Stack, hot: FreshDb) -> None:
    async with client_for(stack, hot) as http:
        r = await http.get("/billing/v1/me/models", headers={"X-User-Id": str(uuid4())})
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] == {"code": "free", "source": "free"}  # an unknown user is Free
    assert body["default_group"] == "fast"
    assert body["messages"]["daily_limit"] == 25
    assert (body["messages"]["used_today"], body["messages"]["remaining_today"]) == (0, 25)
    assert body["messages"]["resets_at"].endswith("+05:30")
    assert [g["group"] for g in body["groups"]] == ["fast", "smart", "best", "ultra"]

    fast, smart = group(body, "fast"), group(body, "smart")
    assert fast["allowed"] and all(m["allowed"] for m in fast["models"])
    assert "upgrade_to" not in fast
    assert not smart["allowed"] and smart["upgrade_to"] == "pro"
    allowed_smart = {m["id"] for m in smart["models"] if m["allowed"]}
    assert allowed_smart == {"openai/gpt-oss-120b"}  # Free's one extra model
    assert group(body, "best")["upgrade_to"] == "max"
    assert group(body, "best")["daily_limit"] == 0
    assert group(body, "ultra")["upgrade_to"] is None  # no individual plan has it, no PAYG yet


async def test_ultra_points_to_payg_when_it_is_enabled(stack: Stack, hot: FreshDb) -> None:
    async with client_for(stack, hot, payg=True) as http:
        r = await http.get("/billing/v1/me/models", headers={"X-User-Id": str(uuid4())})
    assert group(r.json(), "ultra")["upgrade_to"] == "payg"


async def test_me_models_for_max_counts_best_down(stack: Stack, hot: FreshDb) -> None:
    uid = await make_user(hot, "max")
    headers = {"X-User-Id": str(uid)}
    async with client_for(stack, hot) as http:
        body = (await http.get("/billing/v1/me/models", headers=headers)).json()
        assert body["plan"] == {"code": "max", "source": "override"}
        assert body["default_group"] == "smart"
        best = group(body, "best")
        assert best["allowed"] and (best["daily_limit"], best["remaining_today"]) == (12, 12)
        assert all(m["allowed"] for m in best["models"])
        assert group(body, "smart")["remaining_today"] == 80

        await stack.reserve.check_and_reserve(msg(uid, group=ModelGroup.BEST))
        body = (await http.get("/billing/v1/me/models", headers=headers)).json()
        assert group(body, "best")["remaining_today"] == 11
        assert body["messages"]["used_today"] == 1
        assert body["messages"]["remaining_today"] == 79
        assert group(body, "smart")["remaining_today"] == 79
        ultra = group(body, "ultra")
        assert not ultra["allowed"] and not any(m["allowed"] for m in ultra["models"])


async def test_best_remaining_never_exceeds_the_daily_remainder(stack: Stack, hot: FreshDb) -> None:
    uid = await make_user(hot, "max")
    for _ in range(79):  # 79 Smart messages of 80: one message left in total
        await stack.reserve.check_and_reserve(msg(uid, group=ModelGroup.SMART))
    async with client_for(stack, hot) as http:
        r = await http.get("/billing/v1/me/models", headers={"X-User-Id": str(uid)})
    assert group(r.json(), "best")["remaining_today"] == 1  # 12 Best left, but only 1 message


async def test_me_models_needs_a_user(stack: Stack, hot: FreshDb) -> None:
    async with client_for(stack, hot) as http:
        for headers in ({}, {"X-User-Id": "not-a-uuid"}):
            r = await http.get("/billing/v1/me/models", headers=headers)
            assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
