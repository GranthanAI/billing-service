"""P1a: every SimulatorGateway method, against real Postgres on a test clock."""

import json
from dataclasses import replace
from typing import Any

import pytest

from billing.adapters import sim_events
from billing.adapters.gateway_sim import SimConfig, SimulatorGateway
from billing.app.errors import GatewayRejected
from billing.app.ports import GatewayPlanSpec, GatewaySubscription
from billing.domain.calendar import ist_day
from billing.domain.money import Paise
from tests.integration.billing_env import Env

PAY = {"method": "card", "card_number": "4111 1111 1111 1111", "otp": "123456"}


def spec(amount: int = 49900, checksum: str = "c1", interval: str = "month") -> GatewayPlanSpec:
    return GatewayPlanSpec(
        "Pro", "monthly", 1, Paise(amount), "INR", {"checksum": checksum, "interval": interval}
    )


def gw(env: Env) -> SimulatorGateway:
    assert isinstance(env.gateway, SimulatorGateway)
    return env.gateway


async def new_sub(env: Env, amount: int = 49900) -> GatewaySubscription:
    g = gw(env)
    plan = await g.ensure_plan(spec(amount, checksum=f"c{amount}"))
    return await g.create_subscription(
        plan, total_count=12, start_at=None, notes={"billing_ref": f"r{len(await _all(env))}"}
    )


async def _all(env: Env) -> list[Any]:
    return list(await env.sql("SELECT id FROM billing_sim.sim_subscriptions"))


def test_ids_have_razorpay_prefix_and_length() -> None:
    for prefix in ("plan", "sub", "pay", "rfnd", "order", "plink", "setl"):
        value = sim_events.new_id(prefix)
        assert value.startswith(f"{prefix}_SIM")
        assert len(value) == len(prefix) + 1 + 14


def test_fee_includes_gst_and_tax_is_inside_it() -> None:
    assert sim_events.fee_of(49900) == (1178, 180)  # same numbers as the Razorpay fixtures
    assert sim_events.fee_of(0) == (0, 0)


def test_config_scale() -> None:
    cfg = SimConfig(month_seconds=60)
    assert replace(cfg, drop_pct=10).drop_pct == 10
    assert cfg.scale == 60 / (30 * 86400) and SimConfig().scale == 1.0


async def test_ensure_plan_is_idempotent_on_checksum_and_interval(sim_env: Env) -> None:
    g = gw(sim_env)
    a = await g.ensure_plan(spec(checksum="x"))
    assert await g.ensure_plan(spec(checksum="x")) == a
    assert await g.ensure_plan(spec(checksum="x", interval="year")) != a
    assert await g.ensure_plan(spec(checksum="y")) != a


async def test_create_and_fetch_subscription(sim_env: Env) -> None:
    g = gw(sim_env)
    plan = await g.ensure_plan(spec())
    notes = {"billing_ref": "ref-1", "user_id": "u1"}
    sub = await g.create_subscription(plan, total_count=12, start_at=None, notes=notes)
    again = await g.create_subscription(plan, total_count=12, start_at=None, notes=notes)
    assert again.id == sub.id  # idempotent on billing_ref
    assert sub.status == "created" and sub.paid_count == 0
    assert sub.short_url and sub.id in sub.short_url
    assert (await g.fetch_subscription(sub.id)).notes["user_id"] == "u1"
    with pytest.raises(GatewayRejected):
        await g.fetch_subscription("sub_nope")
    with pytest.raises(GatewayRejected):
        await g.create_subscription("plan_nope", total_count=1, start_at=None, notes={})


async def test_paying_activates_charges_and_signs_like_razorpay(sim_env: Env) -> None:
    g = gw(sim_env)
    sub = await new_sub(sim_env)
    result = await g.pay(sub.id, **PAY)
    assert result.ok and result.handler is not None
    h = result.handler
    assert g.verify_checkout_signature(
        h["razorpay_payment_id"], h["razorpay_subscription_id"], h["razorpay_signature"]
    )
    assert h["razorpay_signature"] == sim_events.sign(
        "sim_key_secret", f"{h['razorpay_payment_id']}|{sub.id}".encode()
    )
    fresh = await g.fetch_subscription(sub.id)
    assert fresh.status == "active" and fresh.paid_count == 1
    assert fresh.current_start and fresh.current_end and fresh.charge_at == fresh.current_end
    pay = await g.fetch_payment(result.payment_id)
    assert (pay.status, pay.amount, pay.method) == ("captured", 49900, "card")
    assert pay.gateway == "simulator" and (pay.fee, pay.tax) == (1178, 180)
    assert [p.id for p in await g.list_subscription_payments(sub.id)] == [pay.id]
    now = sim_env.clock.now()
    found = await g.list_payments(now.replace(year=2020), now.replace(year=2030))
    assert [p.id for p in found] == [pay.id]


@pytest.mark.parametrize(
    "kwargs",
    [
        {**PAY, "card_number": "4000 0000 0000 0002"},
        {**PAY, "otp": "000000"},
        {**PAY, "otp": "12"},
        {"method": "upi", "vpa": "failure@razorpay", "otp": "123456"},
    ],
)
async def test_failing_instruments_leave_the_subscription_unpaid(
    sim_env: Env, kwargs: dict[str, str]
) -> None:
    g = gw(sim_env)
    sub = await new_sub(sim_env)
    result = await g.pay(sub.id, **kwargs)
    assert not result.ok and result.error
    assert (await g.fetch_payment(result.payment_id)).status == "failed"
    assert (await g.fetch_subscription(sub.id)).status == "created"
    assert await g.list_subscription_payments(sub.id) == []


async def test_upi_and_emandate_succeed_and_upi_is_refused_from_15000(sim_env: Env) -> None:
    g = gw(sim_env)
    upi = await g.pay(
        (await new_sub(sim_env)).id, method="upi", vpa="success@razorpay", otp="654321"
    )
    assert upi.ok
    assert (await g.pay((await new_sub(sim_env, 99900)).id, method="emandate", otp="654321")).ok
    max_year = await new_sub(sim_env, 14_990_00)  # Max yearly is below the limit
    assert "upi" in (await g.checkout_info(max_year.id))["methods"]
    assert (await g.pay(max_year.id, method="upi", vpa="success@razorpay", otp="654321")).ok
    big = await new_sub(sim_env, 15_000_00)
    assert "upi" not in (await g.checkout_info(big.id))["methods"]
    refused = await g.pay(big.id, method="upi", vpa="success@razorpay", otp="654321")
    assert not refused.ok and "15,000" in (refused.error or "")


async def test_a_later_start_only_authenticates_the_mandate(sim_env: Env) -> None:
    g = gw(sim_env)
    plan = await g.ensure_plan(spec())
    start = sim_env.clock.now().replace(year=2027)
    sub = await g.create_subscription(plan, total_count=12, start_at=start, notes={})
    result = await g.pay(sub.id, **PAY)
    fresh = await g.fetch_subscription(sub.id)
    assert result.ok and fresh.status == "authenticated" and fresh.paid_count == 0
    assert fresh.charge_at == start
    assert (await g.fetch_payment(result.payment_id)).status == "authorized"
    assert await g.list_subscription_payments(sub.id) == []


async def test_cancel_immediately_and_at_cycle_end(sim_env: Env) -> None:
    g = gw(sim_env)
    first = await new_sub(sim_env)
    await g.pay(first.id, **PAY)
    assert (await g.cancel_subscription(first.id, at_cycle_end=True)).status == "active"
    other = await new_sub(sim_env, 99900)
    await g.pay(other.id, **PAY)
    gone = await g.cancel_subscription(other.id, at_cycle_end=False)
    assert gone.status == "cancelled" and gone.charge_at is None
    with pytest.raises(GatewayRejected):
        await g.cancel_subscription(other.id, at_cycle_end=False)


async def test_refunds_go_pending_then_processed_and_are_idempotent(sim_env: Env) -> None:
    g = gw(sim_env)
    pay = (await g.pay((await new_sub(sim_env)).id, **PAY)).payment_id
    refund = await g.refund_payment(pay, Paise(10000), notes={"refund_key": "k1"})
    assert refund.status == "pending" and refund.id.startswith("rfnd_SIM")
    assert (await g.refund_payment(pay, Paise(10000), notes={"refund_key": "k1"})).id == refund.id
    with pytest.raises(GatewayRejected):
        await g.refund_payment(pay, Paise(40000), notes={})  # more than is left
    assert await g.process_refunds() == 0  # the delay has not passed
    sim_env.clock.advance(seconds=11)
    assert await g.process_refunds() == 1
    assert (await g.fetch_refund(refund.id)).status == "processed"
    assert (await g.fetch_payment(pay)).raw["amount_refunded"] == 10000
    with pytest.raises(GatewayRejected):
        await g.fetch_refund("rfnd_nope")


async def test_a_pending_refund_can_be_failed(sim_env: Env) -> None:
    g = gw(sim_env)
    pay = (await g.pay((await new_sub(sim_env)).id, **PAY)).payment_id
    refund = await g.refund_payment(pay, Paise(100), notes={})
    assert (await g.fail_refund(refund.id)).status == "failed"
    with pytest.raises(GatewayRejected):
        await g.fail_refund(refund.id)


async def test_orders_are_idempotent_on_receipt_and_verify_like_razorpay(sim_env: Env) -> None:
    g = gw(sim_env)
    order = await g.create_order(Paise(19900), receipt="rc1", notes={"billing_ref": "x"})
    assert (await g.create_order(Paise(19900), receipt="rc1", notes={})).id == order.id
    assert order.status == "created" and order.amount == 19900
    result = await g.pay(order.id, **PAY)
    assert result.ok and result.handler is not None
    sig = result.handler["razorpay_signature"]
    assert g.verify_order_signature(order.id, result.handler["razorpay_payment_id"], sig)
    assert not g.verify_order_signature(order.id, "pay_x", sig)
    assert (await g.fetch_order(order.id)).status == "paid"
    with pytest.raises(GatewayRejected):
        await g.pay(order.id, **PAY)  # already paid


async def test_payment_links_are_idempotent_pay_and_cancel(sim_env: Env) -> None:
    g = gw(sim_env)

    async def make(amount: int, ref: str):  # type: ignore[no-untyped-def]
        return await g.create_payment_link(
            Paise(amount),
            description="Invoice",
            customer_name="Org",
            customer_email=None,
            reference_id=ref,
            notes={},
        )

    link = await make(500000, "inv-1")
    assert (await make(500000, "inv-1")).id == link.id
    assert link.short_url and link.id.startswith("plink_SIM")
    result = await g.pay(link.id, **PAY)
    assert result.ok and result.handler is not None
    h = result.handler
    assert g.verify_payment_link_signature(
        link.id, "inv-1", "paid", h["razorpay_payment_id"], h["razorpay_signature"]
    )
    await g.cancel_payment_link(link.id)  # a paid link stays paid
    other = await make(100, "inv-2")
    await g.cancel_payment_link(other.id)
    await g.cancel_payment_link(other.id)  # twice is harmless
    with pytest.raises(GatewayRejected):
        await g.pay(other.id, **PAY)
    with pytest.raises(GatewayRejected):
        await g.cancel_payment_link("plink_nope")


async def test_settlement_lists_payments_net_of_fees_and_refunds(sim_env: Env) -> None:
    g = gw(sim_env)
    pay = (await g.pay((await new_sub(sim_env)).id, **PAY)).payment_id
    refund = await g.refund_payment(pay, Paise(1000), notes={})
    sim_env.clock.advance(seconds=11)
    await g.process_refunds()
    assert await g.settle() is None  # T+2 days has not passed
    sim_env.clock.advance(days=2)
    assert await g.settle() is not None
    assert await g.settle() is None  # nothing left
    items = {i.entity_id: i for i in await g.list_settlement_items(ist_day(sim_env.clock.now()))}
    assert items[pay].credit == 49900 - 1178 and items[pay].fee == 1178 and items[pay].tax == 180
    assert items[refund.id].kind == "refund" and items[refund.id].debit == 1000
    assert items[refund.id].payment_id == pay
    assert (items[pay].settlement_id or "").startswith("setl_SIM")


async def test_every_state_change_queues_a_signed_webhook(sim_env: Env) -> None:
    g = gw(sim_env)
    await g.pay((await new_sub(sim_env)).id, **PAY)
    rows = await sim_env.sql(
        "SELECT event, body, signature FROM billing_sim.sim_webhook_outbox ORDER BY id"
    )
    assert [r[0] for r in rows] == [
        "subscription.authenticated",
        "subscription.activated",
        "subscription.charged",
    ]
    for _event, body, signature in rows:
        assert g.verify_webhook_signature(bytes(body), signature)
        assert not g.verify_webhook_signature(bytes(body) + b" ", signature)
        assert json.loads(bytes(body))["entity"] == "event"
