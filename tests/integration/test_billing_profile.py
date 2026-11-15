"""F3: the customer's billing profile: validation, future-only effect, audit; and the public GST state list."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from billing.api.rest import me as me_routes
from billing.api.rest import public as public_routes
from billing.api.rest.errors import install as install_errors
from billing.app.billing_profile import BillingProfileService, gst_states, validate
from billing.app.errors import CatalogInvalid
from tests.integration.billing_env import PRO_MONTH, Env

GOOD = "27ABCDE1234F1Z5"


def test_validation_names_each_field_and_cross_checks_the_state() -> None:
    assert validate("a@example.com", "27", GOOD, "Acme Pvt Ltd") == []
    assert validate("a@example.com", "27", None, None) == []  # GSTIN is optional
    assert validate("nope", "27", None, None) == ["billing_email: is not a valid email address"]
    assert validate("a@example.com", "25", None, None) == [
        "state_code: is not a GST state code"
    ]  # 25 was merged into 26
    assert validate("a@example.com", "27", "27ABCDE1234", "x")[0].startswith(
        "gstin: is not a valid GSTIN"
    )
    assert validate("a@example.com", "29", GOOD, "Acme") == [
        "gstin: the first two digits must equal the state code"
    ]
    assert validate("a@example.com", "27", GOOD, " ") == [
        "legal_name: is required when a GSTIN is given"
    ]
    several = validate("bad", "99", GOOD, None)
    assert len(several) >= 3  # all of them at once


def test_the_state_list_is_billings_own_list() -> None:
    states = gst_states()
    assert {"code": "27", "name": "Maharashtra"} in states and {
        "code": "97",
        "name": "Other Territory",
    } in states
    assert "25" not in {s["code"] for s in states}
    assert [s["code"] for s in states] == sorted(s["code"] for s in states)


async def test_a_change_applies_to_future_invoices_only_and_is_audited(env: Env) -> None:
    svc = BillingProfileService(env.bctx.uow_factory)
    options = (
        await env.activate()
    )  # state 10 (Bihar), no GSTIN: the first invoice is issued with that
    first = await env.sql(
        "SELECT number, customer_state_code, customer_gstin FROM billing.invoices"
    )
    assert [tuple(r) for r in first] == [("GA/2627/000001", "10", None)]

    saved = await svc.update(env.user, "billing@acme.example", "27", GOOD.lower(), " Acme Pvt Ltd ")
    assert (saved.state_code, saved.gstin, saved.legal_name) == (
        "27",
        GOOD,
        "Acme Pvt Ltd",
    )  # normalised
    profile = await svc.get(env.user)
    assert (
        profile.billing_email == "billing@acme.example"
        and profile.as_json()["state_name"] == "Maharashtra"
    )

    # the invoice that exists is untouched, even though the change is in the same month
    again = await env.sql(
        "SELECT number, customer_state_code, customer_gstin FROM billing.invoices ORDER BY number"
    )
    assert [tuple(r) for r in again] == [("GA/2627/000001", "10", None)]

    # the next invoice carries the new details (interstate -> IGST)
    env.clock.set(datetime(2026, 12, 1, 4, 45, tzinfo=UTC))
    await env.deliver(env.gateway.charge(options.gateway_subscription_id, PRO_MONTH))
    await env.process()
    rows = await env.sql(
        "SELECT number, customer_state_code, customer_gstin, igst_paise FROM billing.invoices ORDER BY number"
    )
    assert [tuple(r) for r in rows] == [
        ("GA/2627/000001", "10", None, 0),
        ("GA/2627/000002", "27", GOOD, 7612),
    ]

    audit = await env.sql(
        "SELECT actor_id, action, before, after FROM billing.admin_audit WHERE action = 'billing_profile.update'"
    )
    assert len(audit) == 1 and audit[0][0] == env.user
    assert (
        audit[0][2]["state_code"] == "10"
        and audit[0][3]["state_code"] == "27"
        and audit[0][3]["gstin"] == GOOD
    )

    # saving the same values again writes no second audit row; clearing the GSTIN is allowed
    await svc.update(env.user, "billing@acme.example", "27", GOOD, "Acme Pvt Ltd")
    cleared = await svc.update(env.user, "billing@acme.example", "27", None, None)
    assert cleared.gstin is None and cleared.legal_name is None
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.admin_audit WHERE action = 'billing_profile.update'"
        )
        == 2
    )


async def test_an_invalid_update_changes_nothing(env: Env) -> None:
    svc = BillingProfileService(env.bctx.uow_factory)
    await svc.update(env.user, "a@example.com", "10", None, None)
    with pytest.raises(CatalogInvalid) as e:
        await svc.update(env.user, "a@example.com", "29", GOOD, "Acme")
    assert e.value.errors == ["gstin: the first two digits must equal the state code"]
    assert (await svc.get(env.user)).state_code == "10"
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.admin_audit WHERE action = 'billing_profile.update'"
        )
        == 1
    )


async def test_a_user_with_no_account_gets_an_empty_profile(env: Env) -> None:
    empty = await BillingProfileService(env.bctx.uow_factory).get(uuid4())
    assert empty.as_json() == {
        "billing_email": None,
        "state_code": None,
        "state_name": None,
        "gstin": None,
        "legal_name": None,
    }


async def test_over_http_422_lists_every_field_and_the_state_list_is_cacheable(env: Env) -> None:
    app = FastAPI()
    app.include_router(me_routes.router)
    app.include_router(public_routes.router)
    install_errors(app)
    app.state.profile = BillingProfileService(env.bctx.uow_factory)
    h = {"X-User-Id": str(env.user)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/billing/v1/gst-states")
        assert r.status_code == 200 and "max-age=86400" in r.headers["cache-control"]
        assert {"code": "10", "name": "Bihar"} in r.json()["states"]
        bad = await c.put(
            "/billing/v1/me/billing-profile",
            headers=h,
            json={"billing_email": "x", "state_code": "27", "gstin": GOOD},
        )
        assert bad.status_code == 422
        assert {e.split(":")[0] for e in bad.json()["errors"]} == {"billing_email", "legal_name"}
        ok = await c.put(
            "/billing/v1/me/billing-profile",
            headers=h,
            json={
                "billing_email": "a@acme.example",
                "state_code": "27",
                "gstin": GOOD,
                "legal_name": "Acme",
            },
        )
        assert ok.status_code == 200 and ok.json()["state_name"] == "Maharashtra"
        got = await c.get("/billing/v1/me/billing-profile", headers=h)
        assert got.json()["gstin"] == GOOD and got.headers["cache-control"] == "no-store"
