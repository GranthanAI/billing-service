"""The two routes organisation-service calls (/internal/orgs/{id}/subscription and /usage), against a stub OrgBilling:
the token check, the answer shape, the 404 NO_SUBSCRIPTION problem, and that the usage route no longer asks org-service
for a member list."""

from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from billing.api.rest import errors
from billing.api.rest.orgs import internal_router
from billing.app.errors import NoSubscription

TOKEN = "org-service-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORG = uuid4()


class StubOrgBilling:
    def __init__(self) -> None:
        self.subscription: dict[str, Any] | Exception = NoSubscription("none")
        self.usage: list[dict[str, Any]] = []
        self.usage_calls: list[tuple[UUID, str, str]] = []

    async def subscription_view(self, org_id: UUID) -> dict[str, Any]:
        if isinstance(self.subscription, Exception):
            raise self.subscription
        return self.subscription

    async def usage_report(self, org_id: UUID, start: Any, end: Any) -> list[dict[str, Any]]:
        self.usage_calls.append((org_id, str(start), str(end)))
        return self.usage


class ExplodingDirectory:
    async def members(self, org_id: UUID) -> list[UUID]:
        raise AssertionError("the usage route must not ask org-service for its members any more")


def build() -> tuple[FastAPI, StubOrgBilling]:
    app = FastAPI()
    stub = StubOrgBilling()
    app.state.org_billing = stub
    app.state.org_service_token = TOKEN
    app.state.org_directory = ExplodingDirectory()
    errors.install(app)
    app.include_router(internal_router)
    return app, stub


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://billing")


SUB = {"subscription_id": str(uuid4()), "plan_code": "org_pro", "variant": "college", "seats": 120, "pending_seats": 100,
       "status": "active", "period_end": "2027-04-01T00:00:00Z"}


async def test_the_subscription_route_answers_the_agreed_shape() -> None:
    app, stub = build()
    stub.subscription = SUB
    async with client(app) as c:
        r = await c.get(f"/internal/orgs/{ORG}/subscription", headers=AUTH)
    assert r.status_code == 200 and r.json() == SUB
    assert set(r.json()) == {"subscription_id", "plan_code", "variant", "seats", "pending_seats", "status", "period_end"}


async def test_no_subscription_is_a_404_problem_with_its_own_code_and_names_the_org() -> None:
    app, stub = build()
    stub.subscription = NoSubscription(f"organisation {ORG} has no subscription")
    async with client(app) as c:
        r = await c.get(f"/internal/orgs/{ORG}/subscription", headers=AUTH)
    body = r.json()
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "NO_SUBSCRIPTION" and str(ORG) in body["detail"]            # not the generic NOT_FOUND


async def test_the_subscription_route_needs_the_service_token() -> None:
    app, stub = build()
    stub.subscription = SUB
    async with client(app) as c:
        assert (await c.get(f"/internal/orgs/{ORG}/subscription")).status_code == 401
        assert (await c.get(f"/internal/orgs/{ORG}/subscription", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get(f"/internal/orgs/{ORG}/subscription", headers=AUTH)).status_code == 200


async def test_a_bad_org_id_is_a_validation_error_not_a_500() -> None:
    app, _ = build()
    async with client(app) as c:
        assert (await c.get("/internal/orgs/not-a-uuid/subscription", headers=AUTH)).status_code == 422


async def test_the_usage_route_reads_by_organisation_without_asking_for_members() -> None:
    app, stub = build()
    stub.usage = [{"user_id": str(uuid4()), "day": "2026-11-02", "model_group": "fast", "messages": 4, "billable_units": 4}]
    async with client(app) as c:
        r = await c.get(f"/internal/orgs/{ORG}/usage?from=2026-11-01&to=2026-11-07", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"org_id": str(ORG), "from": "2026-11-01", "to": "2026-11-07", "rows": stub.usage}
    assert stub.usage_calls == [(ORG, "2026-11-01", "2026-11-07")]


async def test_an_organisation_with_no_usage_is_an_empty_list_not_an_error() -> None:
    app, stub = build()
    async with client(app) as c:
        r = await c.get(f"/internal/orgs/{ORG}/usage?from=2026-11-01&to=2026-11-07", headers=AUTH)
    assert r.status_code == 200 and r.json()["rows"] == []


async def test_the_usage_csv_is_unchanged() -> None:
    app, stub = build()
    stub.usage = [{"user_id": "u1", "day": "2026-11-03", "model_group": "smart", "messages": 12, "billable_units": 12}]
    async with client(app) as c:
        r = await c.get(f"/internal/orgs/{ORG}/usage?from=2026-11-01&to=2026-11-30&format=csv", headers=AUTH)
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.strip().split("\n") == ["user_id,day,model_group,messages,billable_units", "u1,2026-11-03,smart,12,12"]


async def test_the_usage_route_still_checks_the_dates_and_the_token() -> None:
    app, _ = build()
    async with client(app) as c:
        assert (await c.get(f"/internal/orgs/{ORG}/usage?from=2026-11-07&to=2026-11-01", headers=AUTH)).status_code == 422
        assert (await c.get(f"/internal/orgs/{ORG}/usage?from=2026-11-01&to=2026-11-07")).status_code == 401
