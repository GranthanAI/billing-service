"""/admin/billing/v1: roles, reasons, audit, and every endpoint."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa

from billing.domain.catalog import Interval
from tests.integration.billing_env import PRO_MONTH, Env

ADMIN = "/admin/billing/v1"
STAFF = uuid4()


def staff(role: str = "billing_admin", *, key: bool = True) -> dict[str, str]:
    h = {"X-User-Id": str(STAFF), "X-Staff-Role": role}
    if key:
        h["Idempotency-Key"] = str(uuid4())
    return h


async def audit(env: Env, action: str) -> list[sa.Row[tuple[object, ...]]]:
    return await env.sql(
        "SELECT actor_id, target_type, target_id, reason, before, after FROM billing.admin_audit "
        "WHERE action = :a ORDER BY id",
        a=action,
    )


# --- access --------------------------------------------------------------------------------------


async def test_roles_and_reasons(env: Env) -> None:
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    async with env.api() as http:
        assert (await http.get(f"{ADMIN}/accounts?q=x")).status_code == 401
        no_role = await http.get(f"{ADMIN}/accounts?q=x", headers={"X-User-Id": str(STAFF)})
        assert no_role.status_code == 401
        weird = await http.get(f"{ADMIN}/accounts?q=x", headers=staff("intern"))
        assert weird.status_code == 401
        assert (
            await http.get(f"{ADMIN}/accounts?q=x", headers=staff("support"))
        ).status_code == 200

        body = {"reason": "chargeback"}
        denied = await http.post(
            f"{ADMIN}/accounts/{account}/block", json=body, headers=staff("support")
        )
        assert denied.status_code == 403 and denied.json()["code"] == "FORBIDDEN"
        put_denied = await http.put(
            f"{ADMIN}/fx-rates",
            json={"effective_from": "2026-11-05", "usd_inr": "96", "reason": "x1x"},
            headers=staff("support"),
        )
        assert put_denied.status_code == 403

        for reason in ("", "  ", "ab"):
            r = await http.post(
                f"{ADMIN}/accounts/{account}/block", json={"reason": reason}, headers=staff()
            )
            assert r.status_code == 422 and r.json()["code"] == "VALIDATION_FAILED"
        missing = await http.post(f"{ADMIN}/accounts/{account}/block", json={}, headers=staff())
        assert missing.status_code == 422
        no_key = await http.post(
            f"{ADMIN}/accounts/{account}/block", json=body, headers=staff(key=False)
        )
        assert no_key.status_code == 400  # admin POSTs need an Idempotency-Key too
    assert await audit(env, "account.block") == []  # nothing was written by the rejected calls


async def test_admin_posts_replay_on_the_same_key(env: Env) -> None:
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    headers = staff()
    async with env.api() as http:
        first = await http.post(
            f"{ADMIN}/accounts/{account}/block", json={"reason": "fraud"}, headers=headers
        )
        again = await http.post(
            f"{ADMIN}/accounts/{account}/block", json={"reason": "fraud"}, headers=headers
        )
        assert first.status_code == again.status_code == 200
        assert again.headers["idempotent-replayed"] == "true"
    assert len(await audit(env, "account.block")) == 1


# --- search and detail -----------------------------------------------------------------------------  # noqa: E501


async def test_search_by_every_identifier_and_the_account_detail(env: Env) -> None:
    options = await env.service.checkout(
        env.user, "pro", Interval.MONTH,
        "10", "Searchable.User@example.com", "Acme", "10ABCDE1234F1Z5",
    )  # fmt: skip
    auth, charged, pay_id, _ = env.gateway.pay_checkout(options.gateway_subscription_id, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    account = str(await env.scalar("SELECT id FROM billing.billing_accounts"))
    async with env.api() as http:
        for q in (
            "searchable.user",  # email, case-insensitive, partial
            str(env.user),
            account,
            options.gateway_subscription_id,
            pay_id,
            "GA/2627/000001",
            "10abcde1234f1z5",  # gstin, any case
        ):
            found = (
                await http.get(f"{ADMIN}/accounts", params={"q": q}, headers=staff("support"))
            ).json()
            assert [a["id"] for a in found["accounts"]] == [account], q
        none = (
            await http.get(f"{ADMIN}/accounts", params={"q": "nobody"}, headers=staff("support"))
        ).json()
        assert none == {"accounts": []}
        assert (await http.get(f"{ADMIN}/accounts", headers=staff("support"))).status_code == 422

        detail = (await http.get(f"{ADMIN}/accounts/{account}", headers=staff("support"))).json()
        assert detail["account"]["billing_email"] == "Searchable.User@example.com"
        assert detail["subscriptions"][0]["plan_code"] == "pro"
        assert detail["subscriptions"][0]["status"] == "active"
        assert [p["gateway_payment_id"] for p in detail["payments"]] == [pay_id]
        assert [i["number"] for i in detail["invoices"]] == ["GA/2627/000001"]
        assert detail["history"][0]["to_status"] == "active"
        assert detail["this_month"]["revenue_paise"] == 42288  # the taxable value
        assert detail["this_month"]["margin_micro_inr"] == 42288 * 10_000
        missing = await http.get(f"{ADMIN}/accounts/{uuid4()}", headers=staff("support"))
        assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"


# --- overrides, block, refunds ------------------------------------------------------------------------  # noqa: E501


async def test_overrides_change_the_plan_at_once_and_end_it(env: Env) -> None:
    assert await env.plan_code() == "free"
    expires = (env.clock.now() + timedelta(days=7)).isoformat()
    async with env.api() as http:
        bad = await http.post(
            f"{ADMIN}/overrides",
            json={"owner_type": "user", "owner_id": str(env.user), "plan_code": "nope",
                  "expires_at": expires, "reason": "support gift"},
            headers=staff(),
        )  # fmt: skip
        assert bad.status_code == 422
        past = await http.post(
            f"{ADMIN}/overrides",
            json={"owner_type": "user", "owner_id": str(env.user), "plan_code": "max",
                  "expires_at": (env.clock.now() - timedelta(days=1)).isoformat(), "reason": "oops"},  # noqa: E501
            headers=staff(),
        )  # fmt: skip
        assert past.status_code == 422
        ok = await http.post(
            f"{ADMIN}/overrides",
            json={"owner_type": "user", "owner_id": str(env.user), "plan_code": "max",
                  "expires_at": expires, "reason": "support gift"},
            headers=staff(),
        )  # fmt: skip
        assert ok.status_code == 201
        assert await env.plan_code() == "max"  # the entitlement cache was cleared on commit
        override_id = ok.json()["id"]
        ended = await http.post(
            f"{ADMIN}/overrides/{override_id}/end", json={"reason": "gift over"}, headers=staff()
        )
        assert ended.status_code == 200
        assert await env.plan_code() == "free"
        again = await http.post(
            f"{ADMIN}/overrides/{override_id}/end", json={"reason": "gift over"}, headers=staff()
        )
        assert again.status_code == 200
        unknown = await http.post(
            f"{ADMIN}/overrides/{uuid4()}/end", json={"reason": "gift over"}, headers=staff()
        )
        assert unknown.status_code == 404
    created = await audit(env, "override.create")
    assert (
        len(created) == 1 and created[0].actor_id == STAFF and created[0].reason == "support gift"
    )
    assert created[0].after["plan"] == "max"
    assert len(await audit(env, "override.end")) == 1  # ending twice audits once


async def test_block_and_unblock_set_the_flag_and_the_audit(env: Env) -> None:
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    async with env.api() as http:
        await http.post(
            f"{ADMIN}/accounts/{account}/block", json={"reason": "fraud ring"}, headers=staff()
        )
        row = (await env.sql("SELECT blocked_at, blocked_reason FROM billing.billing_accounts"))[0]
        assert row[0] is not None and row[1] == "fraud ring"
        ent = await env.entitlements.resolve(env.user)
        assert ent.account_blocked is True
        await http.post(
            f"{ADMIN}/accounts/{account}/unblock", json={"reason": "cleared"}, headers=staff()
        )
        row = (await env.sql("SELECT blocked_at, blocked_reason FROM billing.billing_accounts"))[0]
        assert row[0] is None and row[1] is None
        assert (await env.entitlements.resolve(env.user)).account_blocked is False
        missing = await http.post(
            f"{ADMIN}/accounts/{uuid4()}/block", json={"reason": "nope"}, headers=staff()
        )
        assert missing.status_code == 404
    block = await audit(env, "account.block")
    assert block[0].before == {"blocked": False} and block[0].after == {"blocked": True}


async def test_a_refund_is_queued_bounded_and_issued_with_a_credit_note(env: Env) -> None:
    await env.activate()
    payment = str(await env.scalar("SELECT id FROM billing.payments"))
    async with env.api() as http:
        too_much = await http.post(
            f"{ADMIN}/refunds",
            json={"payment_id": payment, "amount_paise": PRO_MONTH + 1, "reason": "goodwill"},
            headers=staff(),
        )
        assert too_much.status_code == 422 and str(PRO_MONTH) in too_much.json()["detail"]
        for amount in (0, -5):
            r = await http.post(
                f"{ADMIN}/refunds",
                json={"payment_id": payment, "amount_paise": amount, "reason": "goodwill"},
                headers=staff(),
            )
            assert r.status_code == 422
        unknown = await http.post(
            f"{ADMIN}/refunds",
            json={"payment_id": str(uuid4()), "amount_paise": 100, "reason": "goodwill"},
            headers=staff(),
        )
        assert unknown.status_code == 404
        ok = await http.post(
            f"{ADMIN}/refunds",
            json={"payment_id": payment, "amount_paise": 10000, "reason": "goodwill"},
            headers=staff(),
        )
        assert ok.status_code == 201 and ok.json()["status"] == "queued"
        await env.run_jobs()
        assert await env.scalar("SELECT status FROM billing.payments") == "partially_refunded"
        assert (
            await env.scalar("SELECT count(*) FROM billing.invoices WHERE kind = 'credit_note'")
            == 1
        )
        again = await http.post(  # only 39,900 is left
            f"{ADMIN}/refunds",
            json={
                "payment_id": payment,
                "amount_paise": PRO_MONTH - 10000 + 1,
                "reason": "goodwill",
            },
            headers=staff(),
        )
        assert again.status_code == 422
    assert len(await audit(env, "refund.request")) == 1


# --- prices, costs, catalog, settlements, reconciliation ---------------------------------------------------  # noqa: E501


async def test_model_prices_and_fx_rates_are_added_with_a_reason(env: Env) -> None:
    price = {
        "model": "brand-new-model", "provider": "groq", "group": "fast",
        "input_usd_per_m": "0.05", "output_usd_per_m": "0.20",
        "effective_from": "2026-11-10T00:00:00Z", "reason": "new model",
    }  # fmt: skip
    fx = {"effective_from": "2026-11-10", "usd_inr": "96.5", "reason": "weekly update"}
    async with env.api() as http:
        assert (
            await http.put(f"{ADMIN}/model-prices", json=price, headers=staff())
        ).status_code == 201
        assert (
            await http.put(f"{ADMIN}/model-prices", json=price, headers=staff())
        ).status_code == 201
        assert (await http.put(f"{ADMIN}/fx-rates", json=fx, headers=staff())).status_code == 201
        assert (await http.put(f"{ADMIN}/fx-rates", json=fx, headers=staff())).status_code == 201
        for bad in (
            {**price, "reason": ""},
            {**price, "input_usd_per_m": "-1"},
            {**price, "group": "turbo"},
        ):
            assert (
                await http.put(f"{ADMIN}/model-prices", json=bad, headers=staff())
            ).status_code == 422
        assert (
            await http.put(f"{ADMIN}/fx-rates", json={**fx, "usd_inr": "0"}, headers=staff())
        ).status_code == 422
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.model_prices WHERE model = 'brand-new-model'"
        )
        == 1
    )
    assert (
        float(
            await env.scalar(
                "SELECT usd_inr FROM billing.fx_rates WHERE effective_from = '2026-11-10'"
            )
        )
        == 96.5
    )  # type: ignore[arg-type]
    assert len(await audit(env, "model_price.put")) == 2


async def test_costs_by_plan_provider_and_user(env: Env) -> None:
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    plan = await env.scalar("SELECT plan_version_id FROM billing.subscriptions")
    at = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    for n, (cost, split) in enumerate(((1000, {"anthropic": 800, "groq": 200}), (500, None))):
        async with env.app_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO billing.usage_events (kind, source_id, occurred_at, user_id, "
                    "billing_account_id, plan_version_id, model, model_group, billable_units, "
                    "cost_micro_inr, price_paise, fx_usd_inr, provider_costs) VALUES "
                    "('message', :s, :t, :u, :a, :p, 'claude-haiku-5-5', 'smart', 1, :c, 0, 95, "
                    "CAST(:pc AS jsonb))"
                ),
                {"s": f"m{n}", "t": at, "u": env.user, "a": account, "p": plan, "c": cost,
                 "pc": json.dumps(split) if split else None},
            )  # fmt: skip
    params = {"from": "2026-11-01", "to": "2026-11-30"}
    async with env.api() as http:
        by = lambda k: http.get(  # noqa: E731
            f"{ADMIN}/costs", params={**params, "by": k}, headers=staff("support")
        )  # noqa: E731
        plan_rows = (await by("plan")).json()["rows"]
        assert [(r["key"], r["cost_micro_inr"], r["events"]) for r in plan_rows] == [
            ("pro", 1500, 2)
        ]
        prov = {r["key"]: r["cost_micro_inr"] for r in (await by("provider")).json()["rows"]}
        # the breakdown where we have it; the answering model's provider (anthropic) where not
        assert prov == {"anthropic": 800 + 500, "groq": 200}
        user_rows = (await by("user")).json()["rows"]
        assert user_rows[0]["key"] == str(env.user) and user_rows[0]["cost_micro_inr"] == 1500
        assert (
            await http.get(f"{ADMIN}/costs", params={**params, "by": "colour"}, headers=staff())
        ).status_code == 422
        reversed_range = {"from": "2026-12-01", "to": "2026-11-01", "by": "plan"}
        assert (
            await http.get(f"{ADMIN}/costs", params=reversed_range, headers=staff())
        ).status_code == 422
        assert (
            await http.get(f"{ADMIN}/costs", params={"from": "2026-11-01"}, headers=staff())
        ).status_code == 422


async def test_apply_limits_moves_live_subscribers_to_the_new_version(env: Env) -> None:
    await env.activate()
    old = await env.scalar("SELECT plan_version_id FROM billing.subscriptions")
    new = uuid4()
    async with env.migrator_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO billing.plan_versions (id, plan_code, variant, version, rank, "
                "price_month_paise, price_year_paise, limits, allowed_models, effective_from, "
                "checksum, default_group) SELECT :n, plan_code, variant, version + 1, rank, "
                "price_month_paise, price_year_paise, limits || '{\"messages_per_day\": 99}', "
                "allowed_models, now(), 'v2', default_group FROM billing.plan_versions WHERE id = :o"  # noqa: E501
            ),
            {"n": new, "o": old},
        )
        audit_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO billing.admin_audit (actor_id, action, target_type, target_id, "
                    "reason, before, after) VALUES (:a, 'catalog.limits_only_change', "
                    "'plan_version', :n, 'sync', CAST(:b AS jsonb), CAST(:f AS jsonb)) RETURNING id"
                ),
                {"a": uuid4(), "n": str(new), "b": json.dumps({"plan_version_id": str(old)}),
                 "f": json.dumps({"plan_version_id": str(new)})},
            )
        ).scalar_one()  # fmt: skip
    assert (await env.entitlements.resolve(env.user)).limits.messages_per_day == 60  # old limits
    async with env.api() as http:
        other = await http.post(
            f"{ADMIN}/catalog/apply-limits/{audit_id + 1000}",
            json={"reason": "roll out"},
            headers=staff(),
        )
        assert other.status_code == 404
        r = await http.post(
            f"{ADMIN}/catalog/apply-limits/{audit_id}", json={"reason": "roll out"}, headers=staff()
        )
        assert r.status_code == 200 and r.json() == {"subscriptions_moved": 1}
        assert (await env.entitlements.resolve(env.user)).limits.messages_per_day == 99
        again = await http.post(
            f"{ADMIN}/catalog/apply-limits/{audit_id}", json={"reason": "roll out"}, headers=staff()
        )
        assert again.json() == {"subscriptions_moved": 0}
    assert (await audit(env, "catalog.apply_limits"))[0].after["subscriptions_moved"] == 1


async def test_manual_settlement_and_reconciliation_reads(env: Env) -> None:
    await env.activate()
    payment = str(await env.scalar("SELECT id FROM billing.payments"))
    body = {
        "payment_id": payment,
        "fee_paise": 998,
        "fee_gst_paise": 180,
        "reason": "bank statement",
    }
    async with env.api() as http:
        first = await http.post(f"{ADMIN}/settlements", json=body, headers=staff())
        again = await http.post(f"{ADMIN}/settlements", json=body, headers=staff())
        assert (first.status_code, first.json()) == (201, {"posted": True})
        assert (again.status_code, again.json()) == (200, {"posted": False})
        unknown = await http.post(
            f"{ADMIN}/settlements", json={**body, "payment_id": str(uuid4())}, headers=staff()
        )
        assert unknown.status_code == 404
        negative = await http.post(
            f"{ADMIN}/settlements", json={**body, "fee_paise": -1}, headers=staff()
        )
        assert negative.status_code == 422
        empty = await http.get(
            f"{ADMIN}/reconciliation", params={"date": "2026-11-01"}, headers=staff("support")
        )
        assert empty.json() == {"date": "2026-11-01", "checks": []}
    bank = await env.scalar(
        "SELECT b.balance_paise FROM billing.ledger_balances b JOIN billing.ledger_accounts a "
        "ON a.id = b.account_id WHERE a.code = 'asset:bank'"
    )
    assert bank == PRO_MONTH - 998 - 180
    assert UUID(payment)


async def test_an_admin_refund_uses_the_interval_of_the_refunded_payment(env: Env) -> None:
    await env.activate("pro", 499000, Interval.YEAR)
    payment = str(await env.scalar("SELECT id FROM billing.payments"))
    async with env.migrator_engine.begin() as conn:  # the subscription has since moved to monthly
        await conn.execute(sa.text("UPDATE billing.subscriptions SET interval = 'month'"))
    async with env.api() as http:
        r = await http.post(
            f"{ADMIN}/refunds",
            json={"payment_id": payment, "amount_paise": 10000, "reason": "goodwill"},
            headers=staff(),
        )
        assert r.status_code == 201
    queued = (await env.sql("SELECT payload FROM billing.jobs WHERE kind = 'refund.issue'"))[0][0]
    assert queued["interval"] == "year"  # from the invoice's 12-month period, not the subscription
    await env.run_jobs()
    reversed_ = await env.sql(
        "SELECT a.code, sum(e.amount_paise) FROM billing.ledger_entries e "
        "JOIN billing.ledger_transactions t ON t.id = e.transaction_id "
        "JOIN billing.ledger_accounts a ON a.id = e.account_id "
        "WHERE t.kind = 'subscription_refund' GROUP BY a.code"
    )
    codes = {r[0]: int(r[1]) for r in reversed_}
    assert codes["liability:deferred_revenue"] > 0  # the yearly charge sat in deferred revenue
    assert "revenue:subscription" not in codes
    assert sum(codes.values()) == 0


async def test_a_monthly_payment_refund_is_booked_against_revenue(env: Env) -> None:
    await env.activate()
    payment = str(await env.scalar("SELECT id FROM billing.payments"))
    async with env.api() as http:
        await http.post(
            f"{ADMIN}/refunds",
            json={"payment_id": payment, "amount_paise": 10000, "reason": "goodwill"},
            headers=staff(),
        )
    assert (await env.sql("SELECT payload FROM billing.jobs WHERE kind = 'refund.issue'"))[0][0][
        "interval"
    ] == "month"
    await env.run_jobs()
    codes = {
        r[0]
        for r in await env.sql(
            "SELECT a.code FROM billing.ledger_entries e "
            "JOIN billing.ledger_transactions t ON t.id = e.transaction_id "
            "JOIN billing.ledger_accounts a ON a.id = e.account_id "
            "WHERE t.kind = 'subscription_refund'"
        )
    }
    assert "revenue:subscription" in codes and "liability:deferred_revenue" not in codes
