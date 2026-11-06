"""REST: the webhook route, the idempotency middleware and the problem+json errors."""

import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from billing.app.errors import GatewayRejected, GatewayUnavailable
from billing.observability import WEBHOOK_REJECTED
from tests.integration.billing_env import PRO_MONTH, Env

WEBHOOK = "/billing/v1/webhooks/razorpay"
CHECKOUT = {"plan_code": "pro", "interval": "month", "state_code": "10", "email": "u@example.com"}


def headers(env: Env, key: str | None = None) -> dict[str, str]:
    h = {"X-User-Id": str(env.user)}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


async def inbox(env: Env) -> int:
    return int(await env.scalar("SELECT count(*) FROM billing.webhook_inbox"))  # type: ignore[arg-type]


# --- webhook route -------------------------------------------------------------------------


async def test_webhook_signature_size_and_dedupe(env: Env) -> None:
    options = await env.checkout()
    hook = env.gateway.authenticate(options.gateway_subscription_id)
    async with env.api() as http:
        rejected = WEBHOOK_REJECTED._value.get()
        bad = await http.post(
            WEBHOOK, content=hook.body, headers={**hook.headers, "X-Razorpay-Signature": "00"}
        )
        assert bad.status_code == 400 and bad.json()["code"] == "INVALID_SIGNATURE"
        assert bad.headers["content-type"].startswith("application/problem+json")
        assert WEBHOOK_REJECTED._value.get() == rejected + 1
        assert await inbox(env) == 0

        no_header = await http.post(WEBHOOK, content=hook.body)
        assert no_header.status_code == 400

        big = b"x" * 1_000_001
        too_big = await http.post(
            WEBHOOK,
            content=big,
            headers={"X-Razorpay-Signature": env.gateway.webhook_secret},  # never even checked
        )
        assert too_big.status_code == 413
        assert await inbox(env) == 0

        ok = await http.post(WEBHOOK, content=hook.body, headers=hook.headers)
        assert ok.status_code == 200
        again = await http.post(WEBHOOK, content=hook.body, headers=hook.headers)
        assert again.status_code == 200
        assert await inbox(env) == 1  # ON CONFLICT DO NOTHING

        # without the event-id header the body's sha256 is the id
        charged = env.gateway.charge(options.gateway_subscription_id, PRO_MONTH)
        plain = {"X-Razorpay-Signature": charged.signature}
        assert (await http.post(WEBHOOK, content=charged.body, headers=plain)).status_code == 200
        assert (await http.post(WEBHOOK, content=charged.body, headers=plain)).status_code == 200
        ids = [r[0] for r in await env.sql("SELECT event_id FROM billing.webhook_inbox")]
        assert hashlib.sha256(charged.body).hexdigest() in ids and len(ids) == 2

        # the stored rows are processed like any other
        await env.process()
        assert await env.status() == "active"


# --- idempotency middleware ----------------------------------------------------------------


async def test_replay_returns_the_stored_response_byte_for_byte(env: Env) -> None:
    key = str(uuid4())
    async with env.api() as http:
        first = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        assert first.status_code == 201
        assert first.json()["razorpay"]["subscription_id"].startswith("sub_fake_")
        second = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        assert second.status_code == 201
        assert second.content == first.content
        assert second.headers["idempotent-replayed"] == "true"
    assert len(env.gateway.called("create_subscription")) == 1


async def test_same_key_with_a_different_body_is_refused(env: Env) -> None:
    key = str(uuid4())
    async with env.api() as http:
        await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        other = await http.post(
            "/billing/v1/checkout", json={**CHECKOUT, "plan_code": "max"}, headers=headers(env, key)
        )
        assert other.status_code == 422 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_running_request_blocks_its_retry(env: Env) -> None:
    key = str(uuid4())
    body = json.dumps({}).encode()
    request_hash = hashlib.sha256(b"POST\n/billing/v1/subscription/cancel\n" + body).hexdigest()
    async with env.uow() as uow:
        await uow.idempotency.begin(str(env.user), key, request_hash, 30)  # first attempt, running
    async with env.api() as http:
        r = await http.post(
            "/billing/v1/subscription/cancel",
            content=body,
            headers={**headers(env, key), "content-type": "application/json"},
        )
        assert r.status_code == 409 and r.json()["code"] == "REQUEST_IN_PROGRESS"


async def test_5xx_responses_are_not_stored(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    key = str(uuid4())
    real = env.gateway.create_subscription

    async def down(*a: object, **k: object) -> object:
        raise GatewayUnavailable("razorpay timed out")

    monkeypatch.setattr(env.gateway, "create_subscription", down)
    async with env.api() as http:
        r = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        assert r.status_code == 503 and r.json()["code"] == "GATEWAY_UNAVAILABLE"
        assert r.json()["correlation_id"] is not None
        assert await env.scalar("SELECT count(*) FROM billing.idempotency_keys") == 0

        async def crash(*a: object, **k: object) -> object:
            raise RuntimeError("boom")

        monkeypatch.setattr(env.gateway, "create_subscription", crash)
        r = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        assert r.status_code == 500
        assert await env.scalar("SELECT count(*) FROM billing.idempotency_keys") == 0

        monkeypatch.setattr(env.gateway, "create_subscription", real)
        ok = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env, key))
        assert ok.status_code == 201  # the same key works once the gateway is back


async def test_gateway_rejection_is_a_502_with_the_gateways_code(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*a: object, **k: object) -> object:
        raise GatewayRejected("BAD_REQUEST_ERROR", "plan is inactive")

    monkeypatch.setattr(env.gateway, "create_subscription", refuse)
    async with env.api() as http:
        r = await http.post(
            "/billing/v1/checkout", json=CHECKOUT, headers=headers(env, str(uuid4()))
        )
        assert r.status_code == 502
        assert "BAD_REQUEST_ERROR" in r.json()["detail"]


async def test_headers_and_validation(env: Env) -> None:
    async with env.api() as http:
        no_key = await http.post("/billing/v1/checkout", json=CHECKOUT, headers=headers(env))
        assert no_key.status_code == 400 and no_key.json()["code"] == "VALIDATION_FAILED"
        not_uuid = await http.post(
            "/billing/v1/checkout", json=CHECKOUT, headers=headers(env, "abc")
        )
        assert not_uuid.status_code == 400
        no_user = await http.post(
            "/billing/v1/checkout", json=CHECKOUT, headers={"Idempotency-Key": str(uuid4())}
        )
        assert no_user.status_code == 401
        bad_user = await http.post(
            "/billing/v1/checkout",
            json=CHECKOUT,
            headers={"X-User-Id": "nope", "Idempotency-Key": str(uuid4())},
        )
        assert bad_user.status_code == 401
        bad_state = await http.post(
            "/billing/v1/checkout",
            json={**CHECKOUT, "state_code": "99"},
            headers=headers(env, str(uuid4())),
        )
        assert bad_state.status_code == 422 and bad_state.json()["code"] == "VALIDATION_FAILED"
        bad_shape = await http.post(
            "/billing/v1/checkout", json={"plan_code": "pro"}, headers=headers(env, str(uuid4()))
        )
        assert bad_shape.status_code == 422
        org = await http.post(
            "/billing/v1/checkout",
            json={**CHECKOUT, "plan_code": "org_pro"},
            headers=headers(env, str(uuid4())),
        )
        assert org.status_code == 409 and org.json()["code"] == "PLAN_NOT_ALLOWED"
        none = await http.post(
            "/billing/v1/subscription/cancel", headers=headers(env, str(uuid4()))
        )
        assert none.status_code == 409 and none.json()["code"] == "SUBSCRIPTION_STATE"
        assert none.json()["type"] == "https://granthan.ai/errors/subscription-state"


async def test_full_flow_over_http(env: Env) -> None:
    async with env.api() as http:
        r = await http.post(
            "/billing/v1/checkout", json=CHECKOUT, headers=headers(env, str(uuid4()))
        )
        sub = r.json()["razorpay"]["subscription_id"]
        _, _, payment_id, signature = env.gateway.pay_checkout(sub, PRO_MONTH)
        verify = await http.post(
            "/billing/v1/checkout/verify",
            json={
                "razorpay_payment_id": payment_id,
                "razorpay_subscription_id": sub,
                "razorpay_signature": signature,
            },
            headers=headers(env, str(uuid4())),
        )
        assert verify.status_code == 200 and verify.json()["status"] == "active"
        bad = await http.post(
            "/billing/v1/checkout/verify",
            json={
                "razorpay_payment_id": payment_id,
                "razorpay_subscription_id": sub,
                "razorpay_signature": "00",
            },
            headers=headers(env, str(uuid4())),
        )
        assert bad.status_code == 400 and bad.json()["code"] == "INVALID_SIGNATURE"
        change = await http.post(
            "/billing/v1/subscription/change",
            json={"plan_code": "max", "interval": "month"},
            headers=headers(env, str(uuid4())),
        )
        assert change.status_code == 201 and change.json()["change"]["kind"] == "upgrade"
        cancel = await http.post(
            "/billing/v1/subscription/cancel", headers=headers(env, str(uuid4()))
        )
        assert cancel.status_code == 200 and cancel.json()["status"] == "cancelling"
        resume = await http.post(
            "/billing/v1/subscription/resume", headers=headers(env, str(uuid4()))
        )
        assert resume.status_code == 201 and resume.json()["change"]["kind"] == "resume"


# --- the trust boundary --------------------------------------------------------------------------


async def test_the_gateway_secret_guards_the_real_routes_and_nothing_runs_without_it(
    env: Env,
) -> None:
    from billing.main_api import app
    from tests.integration.billing_env import GATEWAY_SECRET

    staff_headers = {"X-User-Id": str(uuid4()), "X-Staff-Role": "billing_admin"}
    user_headers = {"X-User-Id": str(env.user), "Idempotency-Key": str(uuid4())}
    calls = [
        ("GET", "/billing/v1/me", user_headers, None),
        ("GET", "/billing/v1/me/models", user_headers, None),
        ("GET", "/billing/v1/invoices", user_headers, None),
        ("POST", "/billing/v1/checkout", user_headers, CHECKOUT),
        ("POST", "/billing/v1/subscription/cancel", user_headers, None),
        ("GET", "/admin/billing/v1/accounts?q=a", staff_headers, None),
        ("POST", f"/admin/billing/v1/accounts/{uuid4()}/block",
         {**staff_headers, "Idempotency-Key": str(uuid4())}, {"reason": "fraud"}),
        ("PUT", "/admin/billing/v1/fx-rates", staff_headers,
         {"effective_from": "2026-11-10", "usd_inr": "96", "reason": "weekly"}),
    ]  # fmt: skip
    async with env.api() as authed:  # installs the services and the secret on the app
        raw = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )
        async with raw:
            for extra in ({}, {"X-Gateway-Secret": "wrong"}, {"X-Gateway-Secret": ""}):
                for method, path, headers, body in calls:
                    r = await raw.request(method, path, headers={**headers, **extra}, json=body)
                    assert r.status_code == 401, (method, path, extra)
                    assert r.json()["code"] == "UNAUTHENTICATED"
            # a staff role sent by a client without the secret gets nothing, whatever it claims
            forged = await raw.get(
                "/admin/billing/v1/accounts?q=a",
                headers={"X-User-Id": str(uuid4()), "X-Staff-Role": "billing_admin"},
            )
            assert forged.status_code == 401

            # public routes still work without it
            assert (await raw.get("/healthz")).status_code == 200
            hook = await raw.post("/billing/v1/webhooks/razorpay", content=b"{}")
            assert hook.status_code == 400 and hook.json()["code"] == "INVALID_SIGNATURE"
            assert (await raw.get("/billing/v1/plans")).status_code != 401
            assert (await raw.get("/billing/v1/models")).status_code != 401

        # nothing ran behind the 401s
        assert await env.scalar("SELECT count(*) FROM billing.idempotency_keys") == 0
        assert await env.scalar("SELECT count(*) FROM billing.billing_accounts") == 0
        assert await env.scalar("SELECT count(*) FROM billing.admin_audit") == 0
        assert env.gateway.called("create_subscription") == []

        # with the secret the same routes are served
        assert (await authed.get("/billing/v1/me", headers=user_headers)).status_code == 200
        listed = await authed.get("/admin/billing/v1/accounts?q=a", headers=staff_headers)
        assert listed.status_code == 200
        assert GATEWAY_SECRET not in listed.text
