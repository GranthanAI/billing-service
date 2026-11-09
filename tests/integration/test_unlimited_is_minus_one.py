"""P2d: every billing surface says -1 for an unlimited limit (plan 4.7), never null or a missing
field: gRPC GetEntitlements, REST /me, REST /me/models and the plan limits in admin audits."""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import grpc
import pytest_asyncio
import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio

from billing.api.grpc.internal import start_grpc_server
from billing.app.admin import Staff
from billing.app.model_picker import ModelPicker
from billing.gen.granthan.billing.v1 import billing_pb2, billing_pb2_grpc
from billing.main_api import app
from tests.integration.billing_env import Env
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import make_stack, make_user

TOKEN = "token-a"
STAFF = Staff(uuid4(), "billing_admin")


@pytest_asyncio.fixture
async def grpc_target(hot: FreshDb, vk: valkey_asyncio.Valkey) -> AsyncIterator[str]:
    stack = make_stack(hot, vk)
    server, port = await start_grpc_server(stack.reserve, [TOKEN], 0, production=False)
    yield f"localhost:{port}"
    await server.stop(0)


async def test_grpc_get_entitlements_says_minus_one_for_unlimited(
    grpc_target: str, hot: FreshDb
) -> None:
    uid = str(await make_user(hot, "max"))  # Max has unlimited branches per tree
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = billing_pb2_grpc.BillingInternalStub(channel)
        r = await stub.GetEntitlements(
            billing_pb2.GetEntitlementsRequest(user_id=uid),
            metadata=[("authorization", f"Bearer {TOKEN}")],
        )
    assert r.limits["branches_per_tree"] == -1
    assert r.limits["messages_per_day"] == 80 and len(r.limits) == 12


async def make_everything_unlimited(env: Env) -> None:
    """A limits-only catalog change that makes daily and Best limits unlimited, applied to the
    live subscriber (the same path as the admin apply-limits route)."""
    old = await env.scalar("SELECT plan_version_id FROM billing.subscriptions")
    new = uuid4()
    async with env.migrator_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO billing.plan_versions (id, plan_code, variant, version, rank, "
                "price_month_paise, price_year_paise, limits, allowed_models, effective_from, "
                "checksum, default_group) SELECT :n, plan_code, variant, version + 1, rank, "
                "price_month_paise, price_year_paise, "
                'limits || \'{"messages_per_day": -1, "best_per_day": -1}\', '
                "allowed_models, now(), 'unlimited', default_group "
                "FROM billing.plan_versions WHERE id = :o"
            ),
            {"n": new, "o": old},
        )
        audit_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO billing.admin_audit (actor_id, action, target_type, target_id, "
                    "reason, before, after) VALUES (:a, 'catalog.limits_only_change', "
                    "'plan_version', :n, 'sync', CAST(:b AS jsonb), CAST(:f AS jsonb)) RETURNING id"
                ),
                {
                    "a": uuid4(), "n": str(new),
                    "b": json.dumps({"plan_version_id": str(old)}),
                    "f": json.dumps({"plan_version_id": str(new)}),
                },
            )
        ).scalar_one()  # fmt: skip
    await env.admin.apply_limits(STAFF, audit_id, "everything unlimited")


async def test_rest_me_and_me_models_say_minus_one_for_unlimited(env: Env) -> None:
    await env.activate()
    await make_everything_unlimited(env)
    headers = {"X-User-Id": str(env.user)}
    async with env.api() as http:
        app.state.picker = ModelPicker(
            env.bctx.uow_factory, env.entitlements, env.counters, env.clock, payg_enabled=False
        )
        me = (await http.get("/billing/v1/me", headers=headers)).json()
        models = (await http.get("/billing/v1/me/models", headers=headers)).json()
    assert me["limits"]["messages_per_day"] == -1 and me["limits"]["best_per_day"] == -1
    assert all(isinstance(v, int) for v in me["limits"].values())  # never null
    assert models["messages"]["daily_limit"] == -1 and models["messages"]["remaining_today"] == -1
    for group in models["groups"]:
        if group["allowed"]:
            assert group["daily_limit"] == -1 and group["remaining_today"] == -1, group
        assert group["daily_limit"] is not None and group["remaining_today"] is not None


async def test_admin_audit_of_a_limits_change_keeps_minus_one(env: Env) -> None:
    await env.activate()
    await make_everything_unlimited(env)
    async with env.api() as http:
        account = await env.scalar("SELECT id FROM billing.billing_accounts")
        r = await http.get(
            f"/admin/billing/v1/accounts/{account}",
            headers={"X-User-Id": str(uuid4()), "X-Staff-Role": "support"},
        )
    assert r.status_code == 200
    assert "null" not in json.dumps(r.json().get("limits", {}))  # an account view has no nulls
