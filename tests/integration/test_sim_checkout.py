"""P1c: the simulated checkout over HTTP, the control API and the user-facing fields."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest_asyncio

from billing.adapters.gateway_sim import SimulatorGateway
from billing.api.rest.sim import install_sim_routes, remove_sim_routes
from billing.main_api import app
from tests.integration.billing_env import Env, pump_webhooks

CHECKOUT = {"plan_code": "pro", "interval": "month", "state_code": "10", "email": "u@example.com"}
PAY = {"method": "card", "card_number": "4111 1111 1111 1111", "otp": "123456"}
STAFF = {"X-Staff-Role": "billing_admin"}


@pytest_asyncio.fixture
async def sim_http(sim_env: Env) -> AsyncIterator[tuple[Env, httpx.AsyncClient]]:
    """The API with the simulator routes mounted, called through the gateway-secret client."""
    async with sim_env.api() as http:
        install_sim_routes(app)
        yield sim_env, http
        remove_sim_routes(app)


def user_headers(env: Env) -> dict[str, str]:
    return {"X-User-Id": str(env.user), "Idempotency-Key": str(uuid4())}


def public(env: Env) -> httpx.AsyncClient:
    """What a browser sends: no gateway secret."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def path_of(url: str) -> str:
    return url.replace("/api/v1/billing/", "/billing/v1/")


async def start_checkout(env: Env, http: httpx.AsyncClient) -> dict[str, object]:
    r = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=user_headers(env))
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


async def test_full_http_checkout_gets_a_free_user_to_pro_with_an_si_invoice(
    sim_http: tuple[Env, httpx.AsyncClient],
) -> None:
    env, http = sim_http
    body = await start_checkout(env, http)
    assert body["provider"] == "simulator"
    url = str(body["simulator_url"])
    assert url.startswith("/api/v1/billing/sim/checkout/sub_SIM") and "?t=" in url

    async with public(env) as browser:
        page = await browser.get(path_of(url))
        assert page.status_code == 200 and "SIMULATED PAYMENT - no real money" in page.text
        assert "499.00" in page.text and "UPI" in page.text and "granthan-sim-checkout" in page.text
        paid = await browser.post(path_of(url).replace("?t=", "/pay?t="), json=PAY)
    assert paid.status_code == 200, paid.text
    handler = paid.json()
    assert set(handler) == {"razorpay_payment_id", "razorpay_subscription_id", "razorpay_signature"}

    verified = await http.post(
        "/billing/v1/checkout/verify", json=handler, headers=user_headers(env)
    )
    assert verified.status_code == 200 and verified.json()["status"] == "active"
    await pump_webhooks(env)
    await env.process()
    await env.run_jobs()

    me = (await http.get("/billing/v1/me", headers=user_headers(env))).json()
    assert me["plan"]["code"] == "pro" and me["status"] == "active"
    assert me["payment_mode"] == "simulated" and me["payment_action_url"] is None
    assert me["limits"]["branches_per_tree"] != 0 and set(me["limits"]) >= {"messages_per_day"}
    invoices = (await http.get("/billing/v1/invoices", headers=user_headers(env))).json()
    numbers = [i["number"] for i in invoices["invoices"]]
    assert len(numbers) == 1 and numbers[0].startswith("SI/")
    gateway = await env.scalar("SELECT gateway FROM billing.payments")
    assert gateway == "simulator"


async def test_a_failing_card_over_http_is_402_and_leaves_the_user_free(
    sim_http: tuple[Env, httpx.AsyncClient],
) -> None:
    env, http = sim_http
    url = path_of(str((await start_checkout(env, http))["simulator_url"]))
    async with public(env) as browser:
        bad = await browser.post(
            url.replace("?t=", "/pay?t="), json={**PAY, "card_number": "4000000000000002"}
        )
        otp = await browser.post(url.replace("?t=", "/pay?t="), json={**PAY, "otp": "000000"})
    assert bad.status_code == 402 and otp.status_code == 402 and "error" in bad.json()
    me = (await http.get("/billing/v1/me", headers=user_headers(env))).json()
    assert me["plan"]["code"] == "free"


async def test_tampered_or_expired_tokens_are_403(sim_http: tuple[Env, httpx.AsyncClient]) -> None:
    env, http = sim_http
    url = path_of(str((await start_checkout(env, http))["simulator_url"]))
    async with public(env) as browser:
        assert (await browser.get(url + "x")).status_code == 403
        assert (await browser.get(url.split("?")[0])).status_code == 403
        other = url.split("/")[-1].split("?")[0]
        forged = url.replace(other, "sub_SIMforgedforge")
        assert (await browser.get(forged)).status_code == 403
        pay_url = url.replace("?t=", "/pay?t=")
        assert (await browser.post(pay_url + "x", json=PAY)).status_code == 403
        env.clock.advance(minutes=31)  # the token lives for 30 minutes
        assert (await browser.get(url)).status_code == 403
        assert (await browser.post(pay_url, json=PAY)).status_code == 403


async def test_upi_is_hidden_from_15000(sim_http: tuple[Env, httpx.AsyncClient]) -> None:
    env, http = sim_http
    gateway = env.gateway
    assert isinstance(gateway, SimulatorGateway)
    order = await gateway.create_order(1_500_000, receipt="big", notes={})  # type: ignore[arg-type]
    token = gateway.make_token(order.id, str(env.user))
    async with public(env) as browser:
        page = await browser.get(f"/billing/v1/sim/checkout/{order.id}?t={token}")
    assert page.status_code == 200 and "UPI" not in page.text and "15,000.00" in page.text


async def test_past_due_me_shows_a_signed_pay_page_that_restores_the_subscription(
    sim_http: tuple[Env, httpx.AsyncClient],
) -> None:
    env, http = sim_http
    gateway = env.gateway
    assert isinstance(gateway, SimulatorGateway)
    body = await start_checkout(env, http)
    gid = str(body["simulator_url"]).split("/")[-1].split("?")[0]
    await gateway.pay(gid, **PAY)
    await pump_webhooks(env)
    await env.process()
    await gateway.set_next_outcome(gid, "fail")
    end = (await gateway.fetch_subscription(gid)).current_end
    assert end is not None
    env.clock.set(end)
    r = await http.post(f"/billing/v1/sim/subscriptions/{gid}/renew-now", headers=STAFF)
    assert r.json()["status"] == "pending"
    await pump_webhooks(env)
    await env.process()
    me = (await http.get("/billing/v1/me", headers=user_headers(env))).json()
    assert me["status"] == "past_due" and me["payment_action_url"].startswith(
        "/api/v1/billing/sim/checkout/sub_SIM"
    )
    async with public(env) as browser:
        pay_url = path_of(me["payment_action_url"]).replace("?t=", "/pay?t=")
        assert (await browser.post(pay_url, json=PAY)).status_code == 200
    await pump_webhooks(env)
    await env.process()
    me = (await http.get("/billing/v1/me", headers=user_headers(env))).json()
    assert me["status"] == "active" and me["payment_action_url"] is None


async def test_control_api_needs_the_staff_role_and_works(
    sim_http: tuple[Env, httpx.AsyncClient],
) -> None:
    env, http = sim_http
    body = await start_checkout(env, http)
    gid = str(body["simulator_url"]).split("/")[-1].split("?")[0]
    gateway = env.gateway
    assert isinstance(gateway, SimulatorGateway)
    pay = await gateway.pay(gid, **PAY)
    await pump_webhooks(env)
    await env.process()

    nxt = f"/billing/v1/sim/subscriptions/{gid}/next-charge"
    assert (await http.post(nxt, json={"outcome": "fail"})).status_code == 401  # no staff role
    assert (
        await http.post(nxt, json={"outcome": "fail"}, headers={"X-Staff-Role": "support"})
    ).status_code == 401
    assert (await http.post(nxt, json={"outcome": "fail"}, headers=STAFF)).status_code == 200
    assert (await http.post(nxt, json={"outcome": "nope"}, headers=STAFF)).status_code == 422

    refund = await gateway.refund_payment(pay.payment_id, 100, notes={})  # type: ignore[arg-type]
    failed = await http.post(f"/billing/v1/sim/refunds/{refund.id}/fail", headers=STAFF)
    assert failed.json()["status"] == "failed"
    assert (
        await http.post("/billing/v1/sim/refunds/rfnd_nope/fail", headers=STAFF)
    ).status_code == 404

    settled = await http.post("/billing/v1/sim/settle-now", headers=STAFF)
    assert settled.status_code == 200 and settled.json()["settlement_id"].startswith("setl_SIM")

    state = (await http.get(f"/billing/v1/sim/state?user_id={env.user}", headers=STAFF)).json()
    assert [s["id"] for s in state["subscriptions"]] == [gid]
    assert state["subscriptions"][0]["next_outcome"] == "fail"
    assert len(state["payments"]) == 1 and len(state["refunds"]) == 1
    assert (await http.get(f"/billing/v1/sim/state?user_id={env.user}")).status_code == 401

    cancel = await http.post(f"/billing/v1/sim/subscriptions/{gid}/customer-cancel", headers=STAFF)
    assert cancel.json()["status"] == "cancelled"


async def test_the_secret_still_guards_the_control_api(
    sim_http: tuple[Env, httpx.AsyncClient],
) -> None:
    env, _ = sim_http
    async with public(env) as browser:
        r = await browser.get(f"/billing/v1/sim/state?user_id={env.user}", headers=STAFF)
        assert r.status_code == 401  # no X-Gateway-Secret: only the checkout pages are public


async def test_sim_routes_are_404_when_simulation_is_off(env: Env) -> None:
    async with env.api() as http:
        page = await http.get("/billing/v1/sim/checkout/sub_x?t=1")
        assert page.status_code == 404
        assert (await http.get("/billing/v1/sim/state?user_id=u", headers=STAFF)).status_code == 404
        r = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=user_headers(env))
        assert r.json()["provider"] == "razorpay" and "simulator_url" not in r.json()
        assert "provider" not in r.json()["razorpay"]
    async with public(env) as browser:  # and without the secret the page is not public either
        assert (await browser.get("/billing/v1/sim/checkout/sub_x?t=1")).status_code == 401


async def test_razorpay_mode_responses_gain_only_provider(env: Env) -> None:
    async with env.api() as http:
        r = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=user_headers(env))
        body = r.json()
        assert body["provider"] == "razorpay"
        assert set(body) == {"provider", "subscription_id", "status", "razorpay"}
        me = (await http.get("/billing/v1/me", headers=user_headers(env))).json()
        assert me["payment_mode"] == "live" and me["payment_action_url"] is None


async def test_healthz_reports_the_payment_mode() -> None:
    from billing.main_api import healthz  # noqa: F401

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    app.state.payment_mode = "simulated"
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/healthz")).json() == {"status": "ok", "payment_mode": "simulated"}
    finally:
        del app.state.payment_mode
