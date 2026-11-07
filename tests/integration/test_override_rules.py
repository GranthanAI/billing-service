"""Fix: overrides never grant Pay as you Go; organisation pilots need org-service."""

from datetime import timedelta
from uuid import uuid4

import pytest
import structlog

from billing.adapters.orgs import FakeOrgDirectory
from billing.app.admin import AdminService, Staff
from billing.app.errors import GatewayUnavailable, ValidationFailed
from billing.app.settlements import SettlementService
from billing.domain.catalog import OwnerType
from tests.integration.billing_env import Env
from tests.integration.test_orgs import make_org
from tests.integration.test_orgs import staff as org_staff

ADMIN = "/admin/billing/v1"
STAFF = Staff(uuid4(), "billing_admin")


def body(owner_type: str, owner_id: object, plan: str, env: Env) -> dict[str, str]:
    return {
        "owner_type": owner_type,
        "owner_id": str(owner_id),
        "plan_code": plan,
        "expires_at": (env.clock.now() + timedelta(days=7)).isoformat(),
        "reason": "support gift",
    }


async def test_payg_override_is_refused_for_user_and_org_with_422(env: Env) -> None:
    org = make_org(env)
    async with env.api() as http:
        for owner_type, owner_id in (("user", env.user), ("org", org.org_id)):
            resp = await http.post(
                f"{ADMIN}/overrides",
                json=body(owner_type, owner_id, "payg", env),
                headers=org_staff(),
            )
            assert resp.status_code == 422
            assert resp.json()["detail"] == (
                "Pay as you Go cannot be granted as an override; top up credits instead"
            )
    assert await env.scalar("SELECT count(*) FROM billing.plan_overrides") == 0


async def test_a_payg_override_row_in_the_database_is_ignored(env: Env) -> None:
    async with env.uow() as uow:
        payg = await uow.plans.current("payg", None)
        assert payg is not None
        from billing.app.records import OverrideRecord

        await uow.overrides.insert(
            OverrideRecord(
                uuid4(), OwnerType.USER, env.user, payg.plan.id, "bad row",
                env.clock.now(), env.clock.now() + timedelta(days=7), uuid4(),
            )
        )  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        assert await env.plan_code() == "free"  # the user's real plan
    assert any(entry["event"] == "payg_override_ignored" for entry in logs)
    ent = await env.entitlements.resolve(env.user)
    assert ent.grant.source.value != "override"


async def test_a_normal_override_still_works(env: Env) -> None:
    async with env.api() as http:
        ok = await http.post(
            f"{ADMIN}/overrides", json=body("user", env.user, "max", env), headers=org_staff()
        )
    assert ok.status_code == 201
    assert await env.plan_code() == "max"


def admin_with(env: Env, orgs: FakeOrgDirectory | None) -> AdminService:
    return AdminService(
        env.bctx.uow_factory,
        env.clock,
        env.bctx.ids,
        SettlementService(env.bctx.uow_factory, env.gateway, env.clock),
        env.entitlements.invalidate,
        orgs,
    )


async def test_org_override_without_org_service_is_refused(env: Env) -> None:
    with pytest.raises(ValidationFailed, match="org-service is required for organisation pilots"):
        await admin_with(env, None).create_override(
            STAFF, OwnerType.ORG, uuid4(), "org_pro", "pilot", env.clock.now() + timedelta(days=5)
        )


async def test_org_override_with_unreachable_org_service_is_503(env: Env) -> None:
    org = make_org(env)
    env.orgs.down = True
    with pytest.raises(GatewayUnavailable):
        await env.admin.create_override(
            STAFF, OwnerType.ORG, org.org_id, "org_pro", "pilot",
            env.clock.now() + timedelta(days=5),
        )  # fmt: skip
    async with env.api() as http:
        resp = await http.post(
            f"{ADMIN}/overrides", json=body("org", org.org_id, "org_pro", env), headers=org_staff()
        )
    assert resp.status_code == 503 and resp.json()["code"] == "GATEWAY_UNAVAILABLE"
    assert await env.scalar("SELECT count(*) FROM billing.plan_overrides") == 0
