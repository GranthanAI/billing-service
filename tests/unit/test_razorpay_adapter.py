"""The Razorpay adapter against mocked HTTP (respx): retries, error mapping, idempotent creates,
signature vectors."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from billing.adapters.razorpay import signatures
from billing.adapters.razorpay.gateway import RazorpayGateway
from billing.app.errors import GatewayRejected, GatewayUnavailable
from billing.app.ports import GatewayPlanSpec
from billing.domain.money import Paise
from tests.support.clock import TestClock

NOW = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
BASE = "https://api.razorpay.com/v1"
SUB = {
    "id": "sub_1",
    "plan_id": "plan_1",
    "status": "created",
    "current_start": None,
    "current_end": None,
    "charge_at": 1793000000,
    "start_at": 1793000000,
    "paid_count": 0,
    "notes": {"billing_ref": "ref-1"},
    "short_url": "https://rzp.io/i/x",
}


def gateway() -> RazorpayGateway:
    async def no_sleep(_: float) -> None:
        return None

    return RazorpayGateway("rzp_id", "rzp_secret", "whsec", TestClock(NOW), sleep=no_sleep)


# --- signatures ----------------------------------------------------------------------------


def test_checkout_signature_vector() -> None:
    expected = "868012839359ee108ea5625aecf941e76f4ab27cfbf9220a01390deecac28712"
    assert (
        signatures.checkout_signature("secret", "pay_29QQoUBi66xm2f", "sub_00000000000001")
        == expected
    )
    assert signatures.verify_checkout(
        "secret", "pay_29QQoUBi66xm2f", "sub_00000000000001", expected
    )
    assert not signatures.verify_checkout("secret", "pay_29QQoUBi66xm2f", "sub_other", expected)
    assert not signatures.verify_checkout("secret", "pay_29QQoUBi66xm2f", "sub_00000000000001", "")


def test_webhook_signature_vectors() -> None:
    fox = b"The quick brown fox jumps over the lazy dog"
    assert (
        signatures.webhook_signature("key", fox)
        == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    )
    body = b'{"event":"subscription.charged"}'
    sig = "52db80d3d38a36885b4695aef907b4aabba72c96e0ed8e66dab33838c6a19f01"
    assert signatures.verify_webhook("whsec", body, sig)
    assert not signatures.verify_webhook("whsec", body + b" ", sig)  # one byte changed
    assert not signatures.verify_webhook("other", body, sig)
    g = gateway()
    assert g.verify_webhook_signature(body, sig)
    assert not g.verify_webhook_signature(body, sig.upper() + "0")


# --- transport -----------------------------------------------------------------------------


@respx.mock
async def test_get_maps_unix_seconds_to_utc_and_uses_basic_auth() -> None:
    route = respx.get(f"{BASE}/subscriptions/sub_1").respond(200, json=SUB)
    sub = await gateway().fetch_subscription("sub_1")
    assert sub.charge_at == datetime.fromtimestamp(1793000000, UTC)
    assert sub.charge_at is not None and sub.charge_at.tzinfo is UTC
    assert sub.current_start is None and sub.paid_count == 0
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
@pytest.mark.parametrize("bad", [429, 500, 502, 503])
async def test_retries_429_and_5xx_then_succeeds(bad: int) -> None:
    route = respx.get(f"{BASE}/subscriptions/sub_1").mock(
        side_effect=[httpx.Response(bad), httpx.Response(bad), httpx.Response(200, json=SUB)]
    )
    assert (await gateway().fetch_subscription("sub_1")).id == "sub_1"
    assert route.call_count == 3


@respx.mock
async def test_retries_network_errors() -> None:
    route = respx.get(f"{BASE}/payments/pay_1").mock(
        side_effect=[
            httpx.ConnectTimeout("t"),
            httpx.ReadError("r"),
            httpx.Response(200, json=_pay()),
        ]
    )
    assert (await gateway().fetch_payment("pay_1")).id == "pay_1"
    assert route.call_count == 3


@respx.mock
async def test_gives_up_after_three_retries() -> None:
    route = respx.get(f"{BASE}/subscriptions/sub_1").respond(503)
    with pytest.raises(GatewayUnavailable):
        await gateway().fetch_subscription("sub_1")
    assert route.call_count == 4  # the try and 3 retries


@respx.mock
async def test_4xx_is_rejected_at_once_with_code_and_description() -> None:
    route = respx.get(f"{BASE}/subscriptions/sub_x").respond(
        400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "No such id"}}
    )
    with pytest.raises(GatewayRejected) as info:
        await gateway().fetch_subscription("sub_x")
    assert (info.value.gateway_code, info.value.description) == ("BAD_REQUEST_ERROR", "No such id")
    assert route.call_count == 1  # a 4xx is never retried


@respx.mock
async def test_non_json_4xx_still_rejects() -> None:
    respx.get(f"{BASE}/subscriptions/sub_x").respond(404, text="not found")
    with pytest.raises(GatewayRejected):
        await gateway().fetch_subscription("sub_x")


# --- subscriptions -------------------------------------------------------------------------


@respx.mock
async def test_create_subscription_body() -> None:
    route = respx.post(f"{BASE}/subscriptions").respond(200, json=SUB)
    start = datetime(2026, 12, 1, tzinfo=UTC)
    await gateway().create_subscription(
        "plan_1", total_count=120, start_at=start, notes={"billing_ref": "ref-1"}
    )
    body = json.loads(route.calls.last.request.content)
    assert body["plan_id"] == "plan_1" and body["total_count"] == 120
    assert body["start_at"] == int(start.timestamp())
    assert body["notes"] == {"billing_ref": "ref-1"}


@respx.mock
async def test_create_subscription_omits_start_at_when_starting_now() -> None:
    route = respx.post(f"{BASE}/subscriptions").respond(200, json=SUB)
    await gateway().create_subscription("plan_1", total_count=1, start_at=None, notes={})
    assert "start_at" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_create_after_a_timeout_finds_the_subscription_it_already_made() -> None:
    post = respx.post(f"{BASE}/subscriptions").mock(side_effect=httpx.ReadTimeout("slow"))
    listing = respx.get(f"{BASE}/subscriptions").respond(
        200,
        json={"items": [{**SUB, "id": "sub_other", "notes": {"billing_ref": "someone-else"}}, SUB]},
    )
    sub = await gateway().create_subscription(
        "plan_1", total_count=120, start_at=None, notes={"billing_ref": "ref-1"}
    )
    assert sub.id == "sub_1"
    assert post.call_count == 1  # no second create
    assert listing.calls.last.request.url.params["plan_id"] == "plan_1"


@respx.mock
async def test_create_retries_when_the_first_attempt_left_nothing_behind() -> None:
    respx.post(f"{BASE}/subscriptions").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=SUB)]
    )
    respx.get(f"{BASE}/subscriptions").respond(200, json={"items": []})
    sub = await gateway().create_subscription(
        "plan_1", total_count=120, start_at=None, notes={"billing_ref": "ref-1"}
    )
    assert sub.id == "sub_1"


@respx.mock
async def test_lookup_first_skips_the_create_when_it_exists() -> None:
    post = respx.post(f"{BASE}/subscriptions").respond(200, json=SUB)
    respx.get(f"{BASE}/subscriptions").respond(200, json={"items": [SUB]})
    sub = await gateway().create_subscription(
        "plan_1", total_count=1, start_at=None, notes={"billing_ref": "ref-1"}, lookup_first=True
    )
    assert sub.id == "sub_1" and post.call_count == 0


@respx.mock
async def test_create_does_not_retry_a_rejection() -> None:
    post = respx.post(f"{BASE}/subscriptions").respond(
        400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "plan inactive"}}
    )
    with pytest.raises(GatewayRejected):
        await gateway().create_subscription(
            "plan_1", total_count=1, start_at=None, notes={"billing_ref": "r"}
        )
    assert post.call_count == 1


@respx.mock
@pytest.mark.parametrize("at_cycle_end,flag", [(True, 1), (False, 0)])
async def test_cancel_sends_the_cycle_end_flag(at_cycle_end: bool, flag: int) -> None:
    route = respx.post(f"{BASE}/subscriptions/sub_1/cancel").respond(
        200, json={**SUB, "status": "cancelled"}
    )
    sub = await gateway().cancel_subscription("sub_1", at_cycle_end=at_cycle_end)
    assert json.loads(route.calls.last.request.content) == {"cancel_at_cycle_end": flag}
    assert sub.status == "cancelled"


# --- payments, refunds, plans --------------------------------------------------------------


def _pay(**kw: object) -> dict[str, object]:
    return {
        "id": "pay_1", "amount": 49900, "currency": "INR", "status": "captured", "method": "upi",
        "fee": 1178, "tax": 180, "order_id": "order_1", "invoice_id": "inv_1",
        "created_at": 1793000000, "notes": [], **kw,
    }  # fmt: skip


@respx.mock
async def test_payment_dto() -> None:
    respx.get(f"{BASE}/payments/pay_1").respond(200, json=_pay())
    p = await gateway().fetch_payment("pay_1")
    assert (p.amount, p.fee, p.tax, p.method, p.currency) == (
        Paise(49900),
        Paise(1178),
        Paise(180),
        "upi",
        "INR",
    )
    assert p.notes == {}  # Razorpay sends [] for empty notes
    assert p.created_at == datetime.fromtimestamp(1793000000, UTC)
    assert p.raw["invoice_id"] == "inv_1"


@respx.mock
async def test_refund_reuses_an_existing_refund_with_the_same_key() -> None:
    post = respx.post(f"{BASE}/payments/pay_1/refund")
    respx.get(f"{BASE}/payments/pay_1/refunds").respond(
        200,
        json={
            "items": [
                {
                    "id": "rfnd_1",
                    "payment_id": "pay_1",
                    "amount": 24950,
                    "status": "processed",
                    "notes": {"refund_key": "k1"},
                }
            ]
        },
    )
    refund = await gateway().refund_payment("pay_1", Paise(24950), notes={"refund_key": "k1"})
    assert refund.id == "rfnd_1" and post.call_count == 0


@respx.mock
async def test_refund_creates_when_the_key_is_new() -> None:
    respx.get(f"{BASE}/payments/pay_1/refunds").respond(200, json={"items": []})
    post = respx.post(f"{BASE}/payments/pay_1/refund").respond(
        200,
        json={
            "id": "rfnd_2",
            "payment_id": "pay_1",
            "amount": 24950,
            "status": "pending",
            "notes": {"refund_key": "k2"},
        },
    )
    refund = await gateway().refund_payment("pay_1", Paise(24950), notes={"refund_key": "k2"})
    assert refund.status == "pending"
    assert json.loads(post.calls.last.request.content)["amount"] == 24950


SPEC = GatewayPlanSpec(
    "Pro", "monthly", 1, Paise(49900), "INR", {"checksum": "abc", "interval": "month"}
)


@respx.mock
async def test_ensure_plan_pages_through_the_list_and_matches_checksum_and_interval() -> None:
    other = {"id": "plan_x", "notes": {"checksum": "zzz", "interval": "month"}}
    wrong_interval = {"id": "plan_y", "notes": {"checksum": "abc", "interval": "year"}}
    match = {"id": "plan_match", "notes": {"checksum": "abc", "interval": "month"}}
    page1 = [other] * 99 + [wrong_interval]
    route = respx.get(f"{BASE}/plans").mock(
        side_effect=[
            httpx.Response(200, json={"items": page1}),
            httpx.Response(200, json={"items": [match]}),
        ]
    )
    post = respx.post(f"{BASE}/plans")
    assert await gateway().ensure_plan(SPEC) == "plan_match"
    assert route.calls[1].request.url.params["skip"] == "100"
    assert post.call_count == 0


@respx.mock
async def test_ensure_plan_creates_when_nothing_matches() -> None:
    respx.get(f"{BASE}/plans").respond(200, json={"items": []})
    post = respx.post(f"{BASE}/plans").respond(200, json={"id": "plan_new"})
    assert await gateway().ensure_plan(SPEC) == "plan_new"
    body = json.loads(post.calls.last.request.content)
    assert body["period"] == "monthly" and body["item"]["amount"] == 49900
    assert body["notes"]["checksum"] == "abc"


@respx.mock
async def test_list_subscription_payments_follows_paid_invoices() -> None:
    respx.get(f"{BASE}/invoices").respond(
        200,
        json={"items": [
            {"id": "inv_1", "status": "paid", "payment_id": "pay_2"},
            {"id": "inv_2", "status": "issued", "payment_id": None},
            {"id": "inv_3", "status": "paid", "payment_id": "pay_1"},
        ]},
    )  # fmt: skip
    respx.get(f"{BASE}/payments/pay_2").respond(200, json=_pay(id="pay_2", created_at=1793100000))
    respx.get(f"{BASE}/payments/pay_1").respond(200, json=_pay(id="pay_1"))
    got = await gateway().list_subscription_payments("sub_1")
    assert [p.id for p in got] == ["pay_2", "pay_1"]  # newest first
