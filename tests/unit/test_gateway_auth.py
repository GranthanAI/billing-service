"""X-Gateway-Secret: every route but the public ones, constant-time, closed when unconfigured."""

import httpx
import pytest
from fastapi import FastAPI

from billing.api.rest.gateway_auth import PUBLIC_PATHS, GatewayAuthMiddleware, secret_matches
from billing.settings import Settings, gateway_secrets

PROTECTED = [
    "/billing/v1/me",
    "/billing/v1/me/models",
    "/billing/v1/me/usage",
    "/billing/v1/invoices",
    "/billing/v1/checkout",
    "/billing/v1/subscription/cancel",
    "/admin/billing/v1/accounts",
    "/admin/billing/v1/refunds",
]
PUBLIC = sorted(PUBLIC_PATHS)


def build(secrets: tuple[str, ...] | None) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    handled: list[str] = []
    if secrets is not None:
        app.state.gateway_secrets = secrets

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT"])
    async def anything(path: str) -> dict[str, str]:
        handled.append(path)
        return {"ok": path}

    app.add_middleware(GatewayAuthMiddleware)
    return app, handled


def client(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers or {}
    )


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
@pytest.mark.parametrize("path", PROTECTED)
async def test_protected_routes_refuse_a_missing_empty_or_wrong_secret(
    path: str, method: str
) -> None:
    app, handled = build(("s3cret",))
    for headers in ({}, {"X-Gateway-Secret": ""}, {"X-Gateway-Secret": "wrong"},
                    {"X-Gateway-Secret": "s3cret "}, {"X-Gateway-Secret": "S3CRET"}):  # fmt: skip
        async with client(app, headers) as http:
            r = await http.request(method, path)
        assert r.status_code == 401, headers
        body = r.json()
        assert body["code"] == "UNAUTHENTICATED" and body["status"] == 401
        assert r.headers["content-type"].startswith("application/problem+json")
        assert "s3cret" not in r.text.lower()
    assert handled == []  # no handler ran


@pytest.mark.parametrize("path", PROTECTED)
async def test_protected_routes_accept_the_secret(path: str) -> None:
    app, handled = build(("s3cret",))
    async with client(app, {"X-Gateway-Secret": "s3cret"}) as http:
        assert (await http.get(path)).status_code == 200
    assert len(handled) == 1


@pytest.mark.parametrize("path", PUBLIC)
async def test_public_routes_need_no_secret(path: str) -> None:
    app, handled = build(("s3cret",))
    async with client(app) as http:
        assert (await http.get(path)).status_code == 200
    assert len(handled) == 1


async def test_either_secret_works_during_a_rotation() -> None:
    app, _ = build(("new-secret", "old-secret"))
    for good in ("new-secret", "old-secret"):
        async with client(app, {"X-Gateway-Secret": good}) as http:
            assert (await http.get("/billing/v1/me")).status_code == 200
    async with client(app, {"X-Gateway-Secret": "older"}) as http:
        assert (await http.get("/billing/v1/me")).status_code == 401


@pytest.mark.parametrize("secrets", [None, ()])
async def test_with_no_secret_configured_everything_protected_is_refused(
    secrets: tuple[str, ...] | None,
) -> None:
    app, handled = build(secrets)
    async with client(app, {"X-Gateway-Secret": ""}) as http:  # even the empty string
        assert (await http.get("/billing/v1/me")).status_code == 401
    async with client(app) as http:
        assert (await http.get("/billing/v1/plans")).status_code == 200  # public still works
    assert handled == ["billing/v1/plans"]


def test_a_secret_is_compared_against_all_of_them() -> None:
    assert secret_matches(b"b", ("a", "b", "c"))
    assert not secret_matches(b"d", ("a", "b", "c"))
    assert not secret_matches(b"", ("a",))
    assert not secret_matches(b"a", ())


def test_the_setting_is_required_and_splits_on_commas() -> None:
    base = dict(
        billing_database_url="x", billing_migration_database_url="x", billing_valkey_url="x",
        kafka_bootstrap_servers="x", razorpay_key_id="k", razorpay_key_secret="s",
        razorpay_webhook_secret="w", service_token_keys="t", supplier_state_code="10",
    )  # fmt: skip
    with pytest.raises(ValueError):
        Settings(**base)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(**base, gateway_shared_secret="")  # type: ignore[arg-type]
    s = Settings(**base, gateway_shared_secret=" new , old,,new ")  # type: ignore[arg-type]
    assert gateway_secrets(s) == ("new", "old")
