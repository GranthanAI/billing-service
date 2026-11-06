"""reconcile.daily: clean days stay clean, and each check catches an injected mismatch."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from billing.app.reconciliation import ReconciliationService, parse_cost_csv
from billing.domain.money import Paise
from billing.observability import RECON_DIFFERENCE
from tests.integration.billing_env import PRO_MONTH, Env

DAY = date(2026, 11, 1)  # the env clock starts at 10:15 IST on this day


def service(env: Env) -> ReconciliationService:
    return ReconciliationService(env.bctx.uow_factory, env.gateway, env.counters, env.clock)


async def run(env: Env, day: date = DAY) -> dict[str, tuple[str, int, dict[str, object]]]:
    results = await service(env).run(day)
    return {r.check: (r.status, r.difference, r.details) for r in results}


async def usage_row(
    env: Env, source: str, at: datetime, costs: dict[str, int] | None, units: int = 1
) -> None:
    account = await env.scalar("SELECT id FROM billing.billing_accounts")
    plan = await env.scalar("SELECT plan_version_id FROM billing.subscriptions")
    async with env.app_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO billing.usage_events (kind, source_id, occurred_at, user_id, "
                "billing_account_id, plan_version_id, model, model_group, billable_units, "
                "cost_micro_inr, price_paise, fx_usd_inr, provider_costs) VALUES "
                "('message', :s, :t, :u, :a, :p, 'claude-haiku-5-5', 'smart', :n, :c, 0, 95, "
                "CAST(:pc AS jsonb))"
            ),
            {"s": source, "t": at, "u": env.user, "a": account, "p": plan,
             "c": sum(costs.values()) if costs else 0, "pc": json.dumps(costs), "n": units},
        )  # fmt: skip


# --- a clean day -----------------------------------------------------------------------------------  # noqa: E501


async def test_a_clean_day_is_ok_everywhere_and_is_recorded(env: Env) -> None:
    await env.activate()
    out = await run(env)
    assert {k: v[0] for k, v in out.items()} == {
        "razorpay_payments": "ok",
        "ledger": "ok",
        "provider_cost": "ok",
        "valkey_counters": "ok",
    }
    assert out["razorpay_payments"][2]["razorpay_total"] == PRO_MONTH
    assert out["razorpay_payments"][2]["ledger_clearing_total"] == PRO_MONTH
    assert out["provider_cost"][2] == {"skipped": "no provider export imported"}
    rows = await env.sql(
        'SELECT "check", status, difference_paise FROM billing.reconciliation_runs'
    )
    assert sorted(r[0] for r in rows) == sorted(out) and {r[1] for r in rows} == {"ok"}
    assert RECON_DIFFERENCE.labels(check="ledger")._value.get() == 0  # type: ignore[attr-defined]


async def test_the_job_runs_for_the_previous_ist_day_and_can_run_twice(env: Env) -> None:
    await env.activate()
    env.clock.set(datetime(2026, 11, 1, 22, 0, tzinfo=UTC))  # 03:30 IST on 2 Nov
    await env.run_job("reconcile.daily")
    await env.run_job("reconcile.daily", {"day": "2026-11-01"})
    rows = await env.sql("SELECT DISTINCT run_date FROM billing.reconciliation_runs")
    assert [r[0] for r in rows] == [DAY]  # both runs were about 1 Nov
    assert await env.scalar("SELECT count(*) FROM billing.reconciliation_runs") == 8
    latest = await env.sql(
        'SELECT count(*) FROM (SELECT DISTINCT ON ("check") id FROM billing.reconciliation_runs '
        'WHERE run_date = :d ORDER BY "check", id DESC) x',
        d=DAY,
    )
    assert latest[0][0] == 4


# --- 1. Razorpay against payments and the clearing account ---------------------------------------------  # noqa: E501


async def test_a_payment_razorpay_has_and_we_do_not_is_flagged(env: Env) -> None:
    await env.activate()
    real = next(iter(env.gateway.payments.values()))
    env.gateway.payments["pay_ghost"] = replace(real, id="pay_ghost", amount=Paise(12345))
    status, difference, details = (await run(env))["razorpay_payments"]
    assert status == "difference" and difference == 12345
    assert details["only_in_razorpay"] == ["pay_ghost"] and details["only_in_payments"] == []
    assert RECON_DIFFERENCE.labels(check="razorpay_payments")._value.get() == 12345  # type: ignore[attr-defined]


async def test_a_payment_we_have_and_razorpay_does_not_and_an_amount_that_differs(env: Env) -> None:
    await env.activate()
    real = next(iter(env.gateway.payments.values()))
    env.gateway.payments[real.id] = replace(real, amount=Paise(PRO_MONTH - 100))
    status, difference, details = (await run(env))["razorpay_payments"]
    assert status == "difference" and difference == -100
    assert details["amount_differs"] == [real.id]
    del env.gateway.payments[real.id]
    status, difference, details = (await run(env))["razorpay_payments"]
    assert status == "difference" and difference == -PRO_MONTH
    assert details["only_in_payments"] == [real.id]


async def test_a_ledger_that_disagrees_with_the_payments_is_flagged(env: Env) -> None:
    await env.activate()
    async with env.migrator_engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE billing.payments SET amount_paise = amount_paise + 7")
        )  # the payment row drifted from both Razorpay and the ledger
    status, difference, _ = (await run(env))["razorpay_payments"]
    assert status == "difference" and difference == -7


# --- 2. the ledger ------------------------------------------------------------------------------------  # noqa: E501


async def test_a_balance_that_is_not_the_sum_of_its_entries_is_flagged(env: Env) -> None:
    await env.activate()
    async with env.migrator_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE billing.ledger_balances SET balance_paise = balance_paise + 5 WHERE "
                "account_id = (SELECT id FROM billing.ledger_accounts "
                "WHERE code = 'asset:razorpay_clearing')"
            )
        )
    status, difference, details = (await run(env))["ledger"]
    assert status == "difference" and difference == 5
    assert details["balance_mismatches"][0]["code"] == "asset:razorpay_clearing"
    assert details["unbalanced_transactions"] == []


async def test_the_database_refuses_an_unbalanced_transaction_so_there_is_nothing_to_inject(
    env: Env,
) -> None:
    await env.activate()
    from sqlalchemy.exc import DBAPIError

    async with env.migrator_engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(
            sa.text(
                "INSERT INTO billing.ledger_entries (transaction_id, account_id, amount_paise) "
                "SELECT t.id, a.id, 1 FROM billing.ledger_transactions t, billing.ledger_accounts a "  # noqa: E501
                "LIMIT 1"
            )
        )
        with pytest.raises(DBAPIError, match="does not balance"):
            await tx.commit()


# --- 3. provider cost -----------------------------------------------------------------------------------  # noqa: E501


async def test_provider_cost_is_flagged_beyond_five_percent_only(env: Env) -> None:
    await env.activate()
    at = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    await usage_row(env, "m1", at, {"anthropic": 80_000, "groq": 20_000})
    await usage_row(env, "m2", at, {"anthropic": 20_000})  # anthropic total 100,000
    async with env.app_engine.begin() as conn:
        for provider, cost in (("anthropic", 104_000), ("groq", 20_000)):  # 4% over, exact
            await conn.execute(
                sa.text(
                    "INSERT INTO billing.provider_costs (provider, day, cost_micro_inr, source) "
                    "VALUES (:p, :d, :c, 'test')"
                ),
                {"p": provider, "d": DAY, "c": cost},
            )
    status, difference, details = (await run(env))["provider_cost"]
    assert status == "ok" and difference == 0 and details["flagged"] == {}

    async with env.app_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE billing.provider_costs SET cost_micro_inr = 106_000 "
                "WHERE provider = 'anthropic'"
            )
        )  # 6% apart
    status, difference, details = (await run(env))["provider_cost"]
    assert status == "difference" and set(details["flagged"]) == {"anthropic"}  # type: ignore[arg-type]
    assert details["flagged"]["anthropic"]["imported"] == 106_000  # type: ignore[index]
    assert difference == 1  # 6,000 micro-rupees is under one paisa, rounded


async def test_a_provider_missing_from_our_ratings_is_flagged(env: Env) -> None:
    await env.activate()
    async with env.app_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO billing.provider_costs (provider, day, cost_micro_inr, source) "
                "VALUES ('openai', :d, 5_000_000, 'test')"
            ),
            {"d": DAY},
        )
    status, difference, details = (await run(env))["provider_cost"]
    assert status == "difference" and "openai" in details["flagged"]  # type: ignore[operator]
    assert difference == 500  # INR 5 = 500 paise


# --- 4. Valkey counters -----------------------------------------------------------------------------------  # noqa: E501


async def test_counters_that_disagree_with_usage_events_are_flagged(env: Env) -> None:
    await env.activate()
    at = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    for n in range(3):
        await usage_row(env, f"c{n}", at, {"anthropic": 1000})
    await env.counters.rebuild_day(env.user, DAY, {"smart": 3})
    status, difference, details = (await run(env))["valkey_counters"]
    assert status == "ok" and details["users_checked"] == 1

    await env.counters.rebuild_day(env.user, DAY, {"smart": 5, "best": 1})  # drifted up
    status, difference, details = (await run(env))["valkey_counters"]
    assert status == "difference" and difference == 3
    assert details["off"] == {str(env.user): {"best": 1, "smart": 2}}

    await env.counters.rebuild_day(env.user, DAY, {})  # lost (Valkey restarted)
    status, difference, details = (await run(env))["valkey_counters"]
    assert status == "difference" and difference == 3
    assert details["off"][str(env.user)] == {"smart": -3}


async def test_failed_attempts_are_not_counted_and_old_days_are_skipped(env: Env) -> None:
    await env.activate()
    # a failed attempt: its cost is recorded but it is not a message (billable_units = 0)
    await usage_row(env, "x", datetime(2026, 11, 1, 6, 0, tzinfo=UTC), {"groq": 10}, units=0)
    assert (await run(env))["valkey_counters"][0] == "ok"  # nothing to count, nothing in Valkey
    env.clock.set(datetime(2026, 11, 20, tzinfo=UTC))
    old = (await run(env))["valkey_counters"]
    assert old[0] == "ok" and old[2] == {"skipped": "counters have expired"}


async def test_a_check_that_cannot_run_is_an_error_not_silence(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_: object) -> list[object]:
        raise RuntimeError("razorpay is down")

    monkeypatch.setattr(env.gateway, "list_payments", down)
    out = await run(env)
    assert out["razorpay_payments"][0] == "error"
    assert "razorpay is down" in str(out["razorpay_payments"][2]["error"])
    assert out["ledger"][0] == "ok"  # the other checks still ran


# --- the CSV import -----------------------------------------------------------------------------------------  # noqa: E501


def test_cost_csv_parsing() -> None:
    rows = parse_cost_csv("day,cost_inr\n2026-11-02,12.50\n2026-11-01,0.000001\n")
    assert rows == [(date(2026, 11, 1), 1), (date(2026, 11, 2), 12_500_000)]
    assert parse_cost_csv("cost_inr,day\n3,2026-11-01\n") == [(date(2026, 11, 1), 3_000_000)]
    for bad in (
        "day,amount\n2026-11-01,1\n",
        "day,cost_inr\nnot-a-date,1\n",
        "day,cost_inr\n2026-11-01,abc\n",
        "day,cost_inr\n2026-11-01,-1\n",
        "day,cost_inr\n2026-11-01,1\n2026-11-01,2\n",
        "",
    ):
        with pytest.raises(ValueError):
            parse_cost_csv(bad)


async def test_importing_a_day_again_replaces_it(env: Env) -> None:
    async with env.uow() as uow:
        await uow.admin.upsert_provider_cost("groq", DAY, 5, "a.csv")
        await uow.admin.upsert_provider_cost("groq", DAY, 9, "b.csv")  # replaces
    rows = await env.sql("SELECT provider, cost_micro_inr, source FROM billing.provider_costs")
    assert [tuple(r) for r in rows] == [("groq", 9, "b.csv")]
