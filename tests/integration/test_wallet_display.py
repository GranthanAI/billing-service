"""Stage G: wallet amounts are shown GST-inclusive; the wallet itself holds the taxable value."""

import json
from dataclasses import replace
from uuid import UUID

from billing.domain.catalog import ModelGroup
from billing.domain.money import Paise, display_paise, taxable_of
from tests.integration.billing_env import Env
from tests.integration.test_wallet import PACK, services, spend, top_up

BEST_PRICE = 400  # paise, GST included
BEST_MODEL = "claude-sonnet-5-5"


async def test_a_199_pack_shows_19900_and_one_best_message_leaves_19500(env: Env) -> None:
    reserve, usage, wallet = services(env)
    await top_up(env, wallet)
    view = await wallet.view(env.user)
    assert view is not None
    assert view["balance_taxable_paise"] == 16864
    assert view["balance_paise"] == 19900

    assert await spend(env, reserve, usage, 1, ModelGroup.BEST, BEST_MODEL) == "credits"
    view = await wallet.view(env.user)
    assert view is not None
    assert view["balance_taxable_paise"] == 16864 - 339 == 16525
    assert view["balance_paise"] == 19500  # 16525 * 1.18 = 19499.5, half up
    assert view["lots"][0]["amount_paise"] == 19900
    assert view["lots"][0]["amount_taxable_paise"] == 16864
    assert view["lots"][0]["remaining_paise"] == 19500


async def test_transactions_show_the_unit_price_not_the_taxable_debit(env: Env) -> None:
    reserve, usage, wallet = services(env)
    await top_up(env, wallet)
    await spend(env, reserve, usage, 1, ModelGroup.BEST, BEST_MODEL)
    rows = await wallet.transactions(env.user)
    assert [(r["kind"], r["amount_paise"], r["amount_taxable_paise"]) for r in rows] == [
        ("payg_usage", -BEST_PRICE, -339),
        ("wallet_topup", PACK, 16864),
    ]


async def test_me_and_the_http_routes_carry_both_amounts(env: Env) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    headers = {"X-User-Id": str(env.user)}
    async with env.api() as http:
        w = (await http.get("/billing/v1/wallet", headers=headers)).json()["wallet"]
        t = (await http.get("/billing/v1/wallet/transactions", headers=headers)).json()
        me = (await http.get("/billing/v1/me", headers=headers)).json()
    assert (w["balance_paise"], w["balance_taxable_paise"]) == (19900, 16864)
    assert (t["transactions"][0]["amount_paise"], t["transactions"][0]["amount_taxable_paise"]) == (
        19900,
        16864,
    )
    assert (me["wallet"]["balance_paise"], me["wallet"]["balance_taxable_paise"]) == (19900, 16864)


async def test_not_gst_registered_shows_the_stored_amount(env: Env) -> None:
    env.with_config(replace(env.bctx.config, gst_registered=False))
    _, _, wallet = services(env)
    await top_up(env, wallet)
    view = await wallet.view(env.user)
    assert view is not None
    assert view["balance_paise"] == view["balance_taxable_paise"] == PACK


async def test_cap_is_typed_gst_inclusive_and_stored_taxable(env: Env) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    await wallet.set_cap(env.user, Paise(11800))
    view = await wallet.view(env.user)
    assert view is not None
    assert (view["monthly_cap_paise"], view["monthly_cap_taxable_paise"]) == (11800, 10000)
    acct = UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
    assert (await env.counters.wallet_get(acct))["cap"] == int(taxable_of(Paise(11800))) == 10000


async def test_credits_expiring_event_carries_both_amounts(env: Env) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    env.clock.advance(days=340)  # inside the 30-day reminder window of a 12-month lot
    await env.run_job("credits.expire")
    rows = await env.sql(
        "SELECT payload FROM billing.outbox WHERE topic = 'wallet.credits_expiring'"
    )
    assert len(rows) == 1
    body = json.loads(bytes(rows[0][0]))
    assert body["remaining_paise"] == 19900
    assert body["remaining_taxable_paise"] == 16864


def test_display_paise_examples() -> None:
    assert display_paise(Paise(16864)) == 19900
    assert display_paise(Paise(16525)) == 19500  # 19499.5 rounds half up
    assert display_paise(Paise(16525), registered=False) == 16525
    assert display_paise(Paise(0)) == 0
