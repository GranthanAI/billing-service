"""PAYG_ENABLED=false switches Pay as you Go off completely; with it on nothing changes
(test_wallet.py is the flag-on suite).

Money was taken the day a top-up arrives, so the webhook path still records the payment;
everything else (routes, grants, the wallet credit,
the reserve) is closed. A cached entitlement from before the flag was turned off must not keep
the grant alive either."""

from uuid import UUID, uuid4

import pytest
import structlog

from billing.app.entitlements import ModelCatalog
from billing.app.errors import NotFound
from billing.app.reserve import CheckAndReserveRequest, ReserveService
from billing.domain.catalog import DenyReason, ModelGroup, OwnerType, ReserveKind
from billing.domain.entitlements import GrantSource
from tests.integration.billing_env import STATE_BIHAR, Env
from tests.integration.test_wallet import PACK, services, top_up

URL = "/billing/v1/wallet"


def headers(env: Env, key: bool = False) -> dict[str, str]:
    h = {"X-User-Id": str(env.user)}
    if key:
        h["Idempotency-Key"] = str(uuid4())
    return h


async def account_id(env: Env) -> UUID:
    return UUID(str(await env.scalar("SELECT id FROM billing.billing_accounts")))


async def test_with_the_flag_off_a_user_with_a_wallet_and_lots_gets_free_entitlements(
    env: Env,
) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    assert await env.plan_code() == "payg"  # flag on: the wallet adds the grant
    assert (
        await env.scalar("SELECT count(*) FROM billing.credit_lots WHERE remaining_paise > 0") == 1
    )

    env.set_payg(False)  # NOT invalidated: the cached PAYG entitlement must be ignored too
    ent = await env.entitlements.resolve(env.user)
    assert ent.grant.source is GrantSource.FREE and ent.grant.plan_version.plan_code == "free"
    assert ent.overflow_wallet_account_id is None

    await env.entitlements.invalidate(env.user)  # and recomputed from scratch it is still Free
    ent = await env.entitlements.resolve(env.user)
    assert ent.grant.source is GrantSource.FREE and ent.overflow_wallet_account_id is None


async def test_with_the_flag_off_an_ultra_request_is_denied_and_the_wallet_is_untouched(
    env: Env,
) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    acct = await account_id(env)
    before = await env.counters.wallet_get(acct)
    env.set_payg(False)
    reserve = ReserveService(  # exactly how production builds it with the flag off: payg=None
        env.entitlements,
        ModelCatalog(env.bctx.uow_factory, env.clock),
        env.counters,
        env.clock,
        env.bctx.ids,
        payg=None,
    )
    r = await reserve.check_and_reserve(
        CheckAndReserveRequest(env.user, "idem-ultra", ReserveKind.MESSAGE, ModelGroup.ULTRA)
    )
    assert not r.allowed and r.deny_reason is DenyReason.MODEL_NOT_IN_PLAN
    assert await env.counters.wallet_get(acct) == before


class Fixed:
    """An entitlement service that still returns the PAYG entitlement (e.g. another replica's stale cache)."""  # noqa: E501

    def __init__(self, ent: object) -> None:
        self._ent = ent

    async def resolve(self, user_id: UUID) -> object:
        return self._ent


class NoCounters:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"counters.{name} must not be reached: deny before any reservation")


@pytest.mark.parametrize(
    "kind,group",
    [
        (ReserveKind.MESSAGE, ModelGroup.ULTRA),
        (ReserveKind.MESSAGE, ModelGroup.SMART),
        (ReserveKind.YOUTUBE_VIDEO, None),
    ],
)
async def test_a_payg_grant_never_falls_back_to_plan_mode_it_is_denied_with_no_credit(
    env: Env, kind: ReserveKind, group: ModelGroup | None
) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    payg_ent = await env.entitlements.resolve(env.user)
    assert payg_ent.grant.source is GrantSource.PAYG

    reserve = ReserveService(
        Fixed(payg_ent),
        ModelCatalog(env.bctx.uow_factory, env.clock),
        NoCounters(),
        env.clock,
        env.bctx.ids,
        payg=None,
    )  # type: ignore[arg-type]
    r = await reserve.check_and_reserve(CheckAndReserveRequest(env.user, "idem-x", kind, group))
    assert not r.allowed and r.deny_reason is DenyReason.NO_CREDIT


async def test_the_top_up_routes_are_404_with_the_flag_off_and_work_with_it_on(env: Env) -> None:
    body = {"pack_code": "ind_199", "state_code": STATE_BIHAR, "email": "u@example.com"}
    verify = {"razorpay_order_id": "o", "razorpay_payment_id": "p", "razorpay_signature": "s"}
    async with env.api() as http:
        on = await http.post(f"{URL}/topups", json=body, headers=headers(env, key=True))
        assert on.status_code == 201 and on.json()["amount_paise"] == PACK  # flag on: unchanged
        assert (await http.get(URL, headers=headers(env))).status_code == 200

        env.set_payg(False)
        for method, path, payload in (
            ("POST", "/topups", body),
            ("POST", "/topups/verify", verify),
            ("PUT", "/settings", {"monthly_cap_paise": 100}),
        ):
            r = await http.request(
                method, f"{URL}{path}", json=payload, headers=headers(env, key=True)
            )
            assert r.status_code == 404, (path, r.text)
            assert r.json()["code"] == "NOT_FOUND"


async def test_with_the_flag_off_the_read_routes_are_404_without_a_wallet_and_keep_working_with_one(
    env: Env,
) -> None:
    async with env.api() as http:
        env.set_payg(False)
        assert (await http.get(URL, headers=headers(env))).status_code == 404
        assert (await http.get(f"{URL}/transactions", headers=headers(env))).status_code == 404

        env.set_payg(True)
        _, _, wallet = services(env)
        await top_up(env, wallet)
        env.set_payg(False)
        r = await http.get(URL, headers=headers(env))  # money is there: the user may still see it
        assert r.status_code == 200 and r.json()["wallet"]["balance_paise"] > 0
        assert (await http.get(f"{URL}/transactions", headers=headers(env))).status_code == 200


async def test_the_service_refuses_top_ups_verification_and_caps_with_the_flag_off(
    env: Env,
) -> None:
    _, _, wallet = services(env)
    env.set_payg(False)
    wallet = type(wallet)(env.bctx, env.counters)
    with pytest.raises(NotFound):
        await wallet.start_topup(
            OwnerType.USER, env.user, pack_code="ind_199", email=None, state_code=STATE_BIHAR
        )
    with pytest.raises(NotFound):
        await wallet.verify(env.user, "o", "p", "s")
    with pytest.raises(NotFound):
        await wallet.set_cap(env.user, None)


async def test_a_late_top_up_payment_is_recorded_without_credit_and_alerts(
    env: Env,
) -> None:
    _, _, wallet = services(env)
    options = await wallet.start_topup(
        OwnerType.USER, env.user, pack_code="ind_199", email="u@example.com", state_code=STATE_BIHAR
    )
    hook, payment_id, _ = env.gateway.pay_order(options.order_id)
    env.set_payg(False)  # flipped off after the customer opened Checkout

    with structlog.testing.capture_logs() as logs:
        await env.deliver(hook)
        await env.process()

    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.payments WHERE purpose = 'topup' AND status = 'captured'"
        )
        == 1
    )  # the money is on record
    assert await env.scalar("SELECT count(*) FROM billing.wallets") == 0
    assert await env.scalar("SELECT count(*) FROM billing.credit_lots") == 0
    assert await env.scalar("SELECT count(*) FROM billing.invoices") == 0
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.ledger_entries e JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code LIKE 'liability:wallet:%'"  # noqa: E501
        )
        == 0
    )
    assert await env.plan_code() == "free"
    alerts = [e for e in logs if e["event"] == "payg_topup_while_disabled"]
    assert len(alerts) == 1 and alerts[0]["log_level"] == "error"
    assert alerts[0]["gateway_payment_id"] == payment_id and alerts[0]["amount_paise"] == PACK

    await env.deliver(hook)  # a replayed webhook records nothing more
    await env.process()
    assert await env.scalar("SELECT count(*) FROM billing.payments WHERE purpose = 'topup'") == 1
