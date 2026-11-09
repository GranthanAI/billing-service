"""Stage G: the model picker offers Pay as you Go (upgrade_to) and credit-funded groups."""

from typing import Any
from uuid import UUID

from billing.app.model_picker import ModelPicker
from tests.integration.billing_env import Env
from tests.integration.test_wallet import services, top_up


def picker(env: Env, *, payg: bool) -> ModelPicker:
    return ModelPicker(
        env.bctx.uow_factory, env.entitlements, env.counters, env.clock, payg_enabled=payg
    )


def group(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(g for g in body["groups"] if g["group"] == name)


async def test_free_user_flag_on_sees_payg_for_ultra_and_max_for_best(env: Env) -> None:
    body = await picker(env, payg=True).for_user(env.user)
    assert body["plan"] == {"code": "free", "source": "free"}
    ultra, best = group(body, "ultra"), group(body, "best")
    assert (ultra["allowed"], ultra["upgrade_to"]) == (False, "payg")
    assert (best["allowed"], best["upgrade_to"]) == (False, "max")  # the lowest plan with Best
    assert "funded_by" not in ultra and "funded_by" not in best
    assert all(m["allowed"] is False for m in ultra["models"]) and len(ultra["models"]) == 2
    fast, smart = group(body, "fast"), group(body, "smart")
    assert fast["allowed"] is True and "upgrade_to" not in fast
    assert (smart["allowed"], smart["upgrade_to"]) == (False, "pro")  # Free has Fast only


async def test_free_user_flag_off_is_unchanged(env: Env) -> None:
    body = await picker(env, payg=False).for_user(env.user)
    ultra, best = group(body, "ultra"), group(body, "best")
    assert (ultra["allowed"], ultra["upgrade_to"]) == (False, None)
    assert (best["allowed"], best["upgrade_to"]) == (False, "max")


async def test_pro_user_with_credits_sees_ultra_funded_by_credits(env: Env) -> None:
    await env.activate("pro")
    _, _, wallet = services(env)
    on = picker(env, payg=True)

    no_credit = await on.for_user(env.user)  # a plan, but no wallet yet
    assert (group(no_credit, "ultra")["allowed"], group(no_credit, "ultra")["upgrade_to"]) == (
        False,
        "payg",
    )

    await top_up(env, wallet)
    body = await on.for_user(env.user)
    assert body["plan"] == {"code": "pro", "source": "subscription"}  # the plan still wins
    ultra = group(body, "ultra")
    assert ultra["allowed"] is True and ultra["funded_by"] == "credits"
    assert ultra["daily_limit"] == -1 and ultra["remaining_today"] == -1
    assert "upgrade_to" not in ultra
    assert [m["allowed"] for m in ultra["models"]] == [True, True]
    smart = group(body, "smart")
    assert smart["allowed"] is True and "funded_by" not in smart  # the plan's own groups

    # flag off: the wallet changes nothing
    off = await picker(env, payg=False).for_user(env.user)
    assert group(off, "ultra")["allowed"] is False and group(off, "ultra")["upgrade_to"] is None

    # credits used up: back to the upgrade offer
    acct = UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
    balance = (await env.counters.wallet_get(acct))["balance"]
    await env.counters.wallet_credit(acct, -balance)
    empty = await on.for_user(env.user)
    assert group(empty, "ultra")["allowed"] is False
    assert group(empty, "ultra")["upgrade_to"] == "payg"
