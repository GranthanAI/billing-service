"""GET /me, /me/usage, /invoices and the invoice PDF link."""

from datetime import UTC, date, datetime
from uuid import uuid4

from billing.domain.catalog import Interval
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env


def h(env: Env, user: object = None) -> dict[str, str]:
    return {"X-User-Id": str(user or env.user)}


async def test_me_for_a_free_user_and_without_a_header(env: Env) -> None:
    async with env.api() as http:
        assert (await http.get("/billing/v1/me")).status_code == 401
        r = await http.get("/billing/v1/me", headers=h(env, uuid4()))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["plan"] == {"code": "free", "version": 1, "interval": None, "source": "free"}
    assert body["status"] == "free" and body["current_period_end"] is None
    assert body["cancel_at_period_end"] is False and body["pending_change"] is None
    assert body["limits"]["messages_per_day"] == 25
    assert body["usage_today"]["total"] == 0 and body["usage_today"]["resets_at"].endswith("+05:30")
    assert body["usage_month"] == {"youtube_videos": 0} and body["wallet"] is None


async def test_me_follows_the_subscription_through_a_downgrade_and_a_cancel(env: Env) -> None:
    await env.activate("max", MAX_MONTH)
    async with env.api() as http:
        body = (await http.get("/billing/v1/me", headers=h(env))).json()
        assert body["plan"]["code"] == "max" and body["plan"]["interval"] == "month"
        assert body["plan"]["source"] == "subscription" and body["status"] == "active"
        assert body["current_period_end"] == "2026-12-01T10:15:00+05:30"
        assert body["limits"]["branches_per_tree"] is None  # unlimited
        assert body["limits"]["best_per_day"] == 12

        down = await env.service.change_plan(env.user, "pro", Interval.MONTH)
        await env.deliver(env.gateway.authenticate(down.gateway_subscription_id))
        await env.process()
        body = (await http.get("/billing/v1/me", headers=h(env))).json()
        assert body["pending_change"] == {
            "kind": "downgrade", "plan_code": "pro", "interval": "month", "status": "authorised",
        }  # fmt: skip

        await env.service.cancel(env.user)
        body = (await http.get("/billing/v1/me", headers=h(env))).json()
        assert body["status"] == "cancelling" and body["cancel_at_period_end"] is True


async def test_usage_is_read_from_usage_daily_for_a_date_range(env: Env) -> None:
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    for day, group, n in ((date(2026, 11, 2), "fast", 3), (date(2026, 11, 2), "smart", 5),
                          (date(2026, 11, 3), "smart", 1), (date(2026, 9, 1), "fast", 9)):  # fmt: skip  # noqa: E501
        async with env.app_engine.begin() as conn:
            import sqlalchemy as sa

            await conn.execute(
                sa.text(
                    "INSERT INTO billing.usage_daily (day, billing_account_id, user_id, "
                    "model_group, messages, billable_units, cost_micro_inr, price_paise) "
                    "VALUES (:d, :a, :u, :g, :n, :n, 100, 0)"
                ),
                {"d": day, "a": account, "u": env.user, "g": group, "n": n},
            )
    async with env.api() as http:
        r = await http.get(
            "/billing/v1/me/usage",
            params={"from": "2026-11-01", "to": "2026-11-30"},
            headers=h(env),
        )
        assert r.status_code == 200 and r.json()["from"] == "2026-11-01"
        assert r.json()["days"] == [
            {"day": "2026-11-02", "groups": {"fast": {"messages": 3, "billable_units": 3},
                                             "smart": {"messages": 5, "billable_units": 5}}},
            {"day": "2026-11-03", "groups": {"smart": {"messages": 1, "billable_units": 1}}},
        ]  # fmt: skip
        assert "cost_micro_inr" not in r.text  # what a message cost us is not the user's business
        default = await http.get("/billing/v1/me/usage", headers=h(env))
        assert default.json()["to"] == "2026-11-01" and default.json()["days"] == []
        for bad in ({"from": "2026-12-01", "to": "2026-11-01"}, {"from": "2020-01-01"}):
            r = await http.get("/billing/v1/me/usage", params=bad, headers=h(env))
            assert r.status_code == 422 and r.json()["code"] == "VALIDATION_FAILED"
        assert (await http.get("/billing/v1/me/usage")).status_code == 401


async def test_invoices_list_newest_first_and_the_pdf_link_is_owner_only(env: Env) -> None:
    await env.activate()
    env.clock.set(datetime(2026, 11, 16, 4, 45, tzinfo=UTC))
    up = await env.service.change_plan(env.user, "max", Interval.MONTH)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    async with env.api() as http:
        queued = (await http.get("/billing/v1/invoices", headers=h(env))).json()["invoices"]
        assert [i["number"] for i in queued] == ["GA/2627/000002", "GA/2627/000001"]
        assert not any(i["pdf_ready"] for i in queued)
        target = queued[1]["id"]
        before = await http.get(f"/billing/v1/invoices/{target}/pdf", headers=h(env))
        assert before.status_code == 404  # still being prepared

        env.clock.advance(minutes=1)  # the refund job runs after the charge
        await env.run_jobs()  # cancels the old mandate, refunds, issues and renders everything
        listed = (await http.get("/billing/v1/invoices", headers=h(env))).json()["invoices"]
        assert [i["number"] for i in listed] == [
            "CN/2627/000001", "GA/2627/000002", "GA/2627/000001",
        ]  # fmt: skip
        note = listed[0]
        assert note["kind"] == "credit_note" and note["original_number"] == "GA/2627/000001"
        assert note["total_paise"] == 24950 and note["status"] == "issued"
        assert listed[2]["total_paise"] == PRO_MONTH and listed[2]["igst_paise"] == 0
        assert all(i["pdf_ready"] for i in listed)
        ok = await http.get(f"/billing/v1/invoices/{target}/pdf", headers=h(env))
        assert ok.status_code == 302
        expected = "https://objects.test/invoices/2627/GA-2627-000001.pdf?expires=300"
        assert ok.headers["location"] == expected
        assert ok.headers["cache-control"] == "no-store"
        assert env.storage.presigned[-1] == ("invoices/2627/GA-2627-000001.pdf", 300)

        stranger = await http.get(f"/billing/v1/invoices/{target}/pdf", headers=h(env, uuid4()))
        assert stranger.status_code == 404  # not 403: the id must not be confirmable
        assert (
            await http.get(f"/billing/v1/invoices/{uuid4()}/pdf", headers=h(env))
        ).status_code == 404
        assert (
            await http.get("/billing/v1/invoices/not-a-uuid/pdf", headers=h(env))
        ).status_code == 404
        assert (await http.get(f"/billing/v1/invoices/{target}/pdf")).status_code == 401
        empty = await http.get("/billing/v1/invoices", headers=h(env, uuid4()))
        assert empty.json() == {"invoices": []}
