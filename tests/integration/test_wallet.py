"""Stage G: Pay as you Go wallet. Top-up, use, exhaust, top-up reconciles to the paisa."""

from pathlib import Path
from uuid import UUID

import pytest

from billing.adapters.catalog.yaml_loader import load_catalog
from billing.app.entitlements import ModelCatalog
from billing.app.errors import InvalidSignature
from billing.app.reconciliation import ReconciliationService  # noqa: F401
from billing.app.reserve import CheckAndReserveRequest, ReserveService
from billing.app.usage import UsageCall, UsageEvent, UsageService
from billing.app.wallet import WalletService
from billing.domain.calendar import ist_day
from billing.domain.catalog import DenyReason, ModelGroup, OwnerType, PaygPrices, ReserveKind
from billing.domain.money import Paise, taxable_of
from tests.integration.billing_env import STATE_BIHAR, Env
from tests.integration.test_reconciliation import run as reconcile

ROOT = Path(__file__).resolve().parents[2]
PACK = 19900
ULTRA = 1200  # paise per Ultra message, GST included
ANSWER = UsageCall("anthropic", "claude-opus-5-5", "answer", 1000, 0, 200)


def payg_prices() -> PaygPrices:
    spec = load_catalog(ROOT / "config/plans.yaml").payg
    return PaygPrices({k: Paise(v) for k, v in spec.unit_prices.items()})


def services(env: Env) -> tuple[ReserveService, UsageService, WalletService]:
    prices = payg_prices()
    reserve = ReserveService(
        env.entitlements,
        ModelCatalog(env.bctx.uow_factory, env.clock),
        env.counters,
        env.clock,
        env.bctx.ids,
        payg=prices,
    )
    usage = UsageService(
        env.bctx.uow_factory, env.entitlements, env.counters, env.clock, payg=prices
    )
    return reserve, usage, WalletService(env.bctx, env.counters)


async def top_up(env: Env, wallet: WalletService, pack: str = "ind_199") -> None:
    options = await wallet.start_topup(
        OwnerType.USER, env.user, pack_code=pack, email="u@example.com", state_code=STATE_BIHAR
    )
    hook, _, _ = env.gateway.pay_order(options.order_id)
    await env.deliver(hook)
    await env.process()


async def spend(env: Env, reserve: ReserveService, usage: UsageService, n: int) -> str:
    """One Ultra message: reserve, then the usage event. Returns funded_by or the deny reason."""
    r = await reserve.check_and_reserve(
        CheckAndReserveRequest(env.user, f"idem-{n}", ReserveKind.MESSAGE, ModelGroup.ULTRA)
    )
    if not r.allowed:
        assert r.deny_reason is not None
        return r.deny_reason.value
    await usage.apply(
        UsageEvent(
            ReserveKind.MESSAGE,
            f"req-{n}",
            f"evt-{n}",
            env.user,
            env.clock.now(),
            "success",
            r.hold_id,
            "ultra",
            "claude-opus-5-5",
            (ANSWER,),
        )  # fmt: skip
    )
    return r.funded_by


async def wallet_ledger(env: Env) -> int:
    return -int(
        await env.scalar(
            "SELECT coalesce(sum(e.amount_paise), 0) FROM billing.ledger_entries e "
            "JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code LIKE 'liability:wallet:%'"  # noqa: E501
        )  # type: ignore[arg-type]
    )


async def lots_remaining(env: Env) -> int:
    return int(
        await env.scalar("SELECT coalesce(sum(remaining_paise), 0) FROM billing.credit_lots")
    )  # type: ignore[arg-type]


async def test_top_up_use_exhaust_top_up_reconciles_to_the_paisa(env: Env) -> None:
    reserve, usage, wallet = services(env)
    assert await wallet.view(env.user) is None

    await top_up(env, wallet)
    taxable = int(taxable_of(Paise(PACK)))
    view = await wallet.view(env.user)
    assert view is not None and view["balance_paise"] == taxable
    assert (
        await env.counters.wallet_get(
            UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
        )
    )["balance"] == taxable
    assert await env.plan_code() == "payg"
    assert await env.scalar("SELECT count(*) FROM billing.invoices WHERE status = 'paid'") == 1

    per_message = int(taxable_of(Paise(ULTRA)))
    sent = 0
    while (outcome := await spend(env, reserve, usage, sent)) == "credits":
        sent += 1
        assert sent < 100
    assert outcome == DenyReason.NO_CREDIT.value
    assert sent == taxable // per_message
    left = taxable - sent * per_message
    assert (await wallet.view(env.user))["balance_paise"] == left  # type: ignore[index]
    assert await wallet_ledger(env) == left == await lots_remaining(env)

    await top_up(env, wallet, "ind_499")
    assert await spend(env, reserve, usage, 1000) == "credits"

    # the mirror, the ledger and the lots agree; nothing is held
    acct = UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
    mirror = await env.counters.wallet_get(acct)
    assert mirror["balance"] == await wallet_ledger(env) == await lots_remaining(env)
    assert mirror.get("held", 0) == 0
    results = await reconcile(env, ist_day(env.clock.now()))
    assert results["razorpay_payments"][0] == "ok" and results["ledger"][0] == "ok"
    assert results["razorpay_payments"][2]["ledger_clearing_total"] == PACK + 49900
    assert (
        await env.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM billing.ledger_entries GROUP BY transaction_id "
            "HAVING sum(amount_paise) <> 0) x"
        )
        == 0
    )


async def test_verify_and_webhook_record_a_top_up_once(env: Env) -> None:
    _, _, wallet = services(env)
    options = await wallet.start_topup(
        OwnerType.USER, env.user, pack_code="ind_199", email=None, state_code=STATE_BIHAR
    )
    hook, payment_id, signature = env.gateway.pay_order(options.order_id)
    with pytest.raises(InvalidSignature):
        await wallet.verify(env.user, options.order_id, payment_id, "bad")
    balance = await wallet.verify(env.user, options.order_id, payment_id, signature)
    await env.deliver(hook)
    await env.process()
    assert balance == int(taxable_of(Paise(PACK)))
    assert (await wallet.view(env.user))["balance_paise"] == balance  # type: ignore[index]
    assert await env.scalar("SELECT count(*) FROM billing.payments WHERE purpose = 'topup'") == 1
    assert await env.scalar("SELECT count(*) FROM billing.credit_lots") == 1


async def test_lots_are_spent_in_expiry_order_and_expiry_posts_breakage(env: Env) -> None:
    reserve, usage, wallet = services(env)
    await top_up(env, wallet)  # lot A, expires first
    env.clock.advance(days=40)
    await top_up(env, wallet, "ind_499")  # lot B
    assert await spend(env, reserve, usage, 1) == "credits"
    rows = await env.sql(
        "SELECT amount_paise - remaining_paise FROM billing.credit_lots ORDER BY expires_at"
    )
    assert rows[0][0] > 0 and rows[1][0] == 0  # the earlier lot paid

    env.clock.advance(days=330)  # lot A is past 12 months, B is not
    await env.run_job("credits.expire")
    assert (
        await env.scalar(
            "SELECT remaining_paise FROM billing.credit_lots ORDER BY expires_at LIMIT 1"
        )
        == 0
    )
    breakage = await env.scalar(
        "SELECT coalesce(sum(e.amount_paise), 0) FROM billing.ledger_entries e "
        "JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code = 'revenue:breakage'"
    )
    assert int(breakage) < 0 and await wallet_ledger(env) == await lots_remaining(env)  # type: ignore[arg-type]


async def test_resync_repairs_a_drifted_mirror_and_the_spend_cap_denies(env: Env) -> None:
    reserve, usage, wallet = services(env)
    await top_up(env, wallet)
    acct = UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))
    await env.counters.wallet_credit(acct, 777)  # drift
    await env.run_job("wallet.resync")
    assert (await env.counters.wallet_get(acct))["balance"] == await wallet_ledger(env)

    await wallet.set_cap(env.user, Paise(int(taxable_of(Paise(ULTRA)))))  # room for one message
    assert await spend(env, reserve, usage, 1) == "credits"
    assert await spend(env, reserve, usage, 2) == DenyReason.SPEND_CAP.value
