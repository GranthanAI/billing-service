"""Stage G: storage.bill, the monthly charge for storage above 10 GiB for Pay as you Go users."""

import json
from uuid import UUID, uuid4

from prometheus_client import REGISTRY

from billing.domain.catalog import OwnerType
from billing.domain.ledger import wallet_code
from billing.domain.money import Paise, display_paise, taxable_of
from billing.jobs.handlers.wallet import GIB
from tests.integration.billing_env import STATE_BIHAR, Env
from tests.integration.test_orgs import balance, unbalanced
from tests.integration.test_wallet import services

MONTH = "202610"  # the job runs on 1 Nov 2026 10:15 IST in the test clock


def billed(result: str) -> float:
    return REGISTRY.get_sample_value("billing_storage_billed_total", {"result": result}) or 0.0


async def payg_user(env: Env, pack: str = "ind_999") -> tuple[UUID, UUID]:
    """A user with a wallet and no plan: (user id, billing account id)."""
    user = uuid4()
    _, _, wallet = services(env)
    options = await wallet.start_topup(
        OwnerType.USER, user, pack_code=pack, email=None, state_code=STATE_BIHAR
    )
    hook, _, _ = env.gateway.pay_order(options.order_id)
    await env.deliver(hook)
    await env.process()
    account = UUID(
        str(await env.scalar("SELECT id FROM billing.billing_accounts WHERE owner_id = :u", u=user))
    )
    return user, account


async def usage_rows(env: Env) -> list[object]:
    return list(
        await env.sql(
            "SELECT source_id, user_id, billable_units, price_paise, shortfall_taxable_paise, "
            "cost_micro_inr FROM billing.usage_events WHERE kind = 'storage' ORDER BY source_id"
        )
    )


async def test_exactly_10_gib_is_free_and_one_byte_over_is_one_gb(env: Env) -> None:
    free_user, _ = await payg_user(env)
    over_user, over_account = await payg_user(env)
    env.storage_usage.used[free_user] = 10 * GIB
    env.storage_usage.used[over_user] = 10 * GIB + 1
    free_before, billed_before = billed("free"), billed("billed")
    start = await env.counters.wallet_get(over_account)

    await env.run_job("storage.bill")

    (row,) = await usage_rows(env)
    assert (row.source_id, row.user_id) == (f"storage:{over_account}:{MONTH}", over_user)  # type: ignore[attr-defined]
    assert (row.billable_units, row.price_paise) == (1, 1000)  # type: ignore[attr-defined]
    assert (row.shortfall_taxable_paise, row.cost_micro_inr) == (0, 0)  # type: ignore[attr-defined]
    taxable = int(taxable_of(Paise(1000)))
    assert taxable == 847
    assert await balance(env, wallet_code(over_account)) == -(int(taxable_of(Paise(99900))) - 847)
    assert (await env.counters.wallet_get(over_account))["balance"] == start["balance"] - 847
    assert billed("free") - free_before == 1 and billed("billed") - billed_before == 1
    assert await unbalanced(env) == 0


async def test_25_5_gib_is_16_gb(env: Env) -> None:
    user, account = await payg_user(env)
    env.storage_usage.used[user] = int(25.5 * GIB)
    await env.run_job("storage.bill")
    (row,) = await usage_rows(env)
    assert (row.billable_units, row.price_paise) == (16, 16000)  # type: ignore[attr-defined]
    assert await balance(env, wallet_code(account)) == -(int(taxable_of(Paise(99900))) - 13559)
    assert int(taxable_of(Paise(16000))) == 13559


async def test_a_rerun_is_a_no_op(env: Env) -> None:
    user, account = await payg_user(env)
    env.storage_usage.used[user] = 12 * GIB
    await env.run_job("storage.bill")
    after_first = await balance(env, wallet_code(account))
    mirror = (await env.counters.wallet_get(account))["balance"]
    dup_before = billed("duplicate")

    await env.run_job("storage.bill")

    assert len(await usage_rows(env)) == 1
    assert await balance(env, wallet_code(account)) == after_first
    assert (await env.counters.wallet_get(account))["balance"] == mirror
    assert billed("duplicate") - dup_before == 1
    ledger_rows = await env.sql(
        "SELECT count(*) FROM billing.ledger_transactions WHERE kind = 'payg_usage'"
    )
    assert ledger_rows[0][0] == 1


async def test_a_shortfall_debits_what_is_there_and_emits_the_event(env: Env) -> None:
    user, account = await payg_user(env, "ind_199")
    wallet_taxable = int(taxable_of(Paise(19900)))
    assert wallet_taxable == 16864
    env.storage_usage.used[user] = 100 * GIB  # 90 GB over: 90,000 paise, 76,271 taxable
    short_before = billed("shortfall")

    await env.run_job("storage.bill")

    (row,) = await usage_rows(env)
    shortfall = int(taxable_of(Paise(90000))) - wallet_taxable
    assert shortfall == 59407
    assert (row.billable_units, row.price_paise, row.shortfall_taxable_paise) == (  # type: ignore[attr-defined]
        90, 90000, shortfall,
    )  # fmt: skip
    assert await balance(env, wallet_code(account)) == 0  # never negative
    assert (await env.counters.wallet_get(account))["balance"] == 0
    assert (
        await env.scalar("SELECT coalesce(sum(remaining_paise), 0) FROM billing.credit_lots") == 0
    )
    events = await env.sql(
        "SELECT payload FROM billing.outbox WHERE topic = 'storage.payment_required'"
    )
    assert len(events) == 1
    body = json.loads(bytes(events[0][0]))
    assert body["user_id"] == str(user) and body["billing_account_id"] == str(account)
    assert (body["over_gb"], body["month"]) == (90, MONTH)
    assert body["shortfall_paise"] == int(display_paise(Paise(shortfall))) == 70100
    assert billed("shortfall") - short_before == 1
    await env.run_job("storage.bill")  # a rerun does not repeat the event
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.outbox WHERE topic = 'storage.payment_required'"
        )
        == 1
    )


async def test_subscription_users_are_skipped(env: Env) -> None:
    await env.activate("pro")  # env.user subscribes, then also tops up a wallet
    _, _, wallet = services(env)
    options = await wallet.start_topup(
        OwnerType.USER, env.user, pack_code="ind_199", email=None, state_code=STATE_BIHAR
    )
    hook, _, _ = env.gateway.pay_order(options.order_id)
    await env.deliver(hook)
    await env.process()
    assert await env.plan_code() == "pro"
    env.storage_usage.used[env.user] = 50 * GIB
    skipped_before = billed("skipped_plan")

    await env.run_job("storage.bill")

    assert await usage_rows(env) == []
    assert env.storage_usage.calls == []  # file-service was not even asked
    assert billed("skipped_plan") - skipped_before == 1


async def test_flag_off_the_job_does_nothing(env: Env) -> None:
    user, _ = await payg_user(env)
    env.storage_usage.used[user] = 50 * GIB
    env.set_payg(False)
    await env.run_job("storage.bill")
    assert await usage_rows(env) == []
    assert env.storage_usage.calls == []
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.processed_events WHERE consumer = 'storage-bill'"
        )
        == 0
    )


async def test_one_user_failing_does_not_stop_the_batch_and_is_billed_on_the_retry(
    env: Env,
) -> None:
    broken, broken_account = await payg_user(env)
    fine, fine_account = await payg_user(env)
    env.storage_usage.used[broken] = env.storage_usage.used[fine] = 11 * GIB
    env.storage_usage.down.add(broken)
    error_before = billed("error")

    await env.run_job("storage.bill")

    sources = [r.source_id for r in await usage_rows(env)]  # type: ignore[attr-defined]
    assert sources == [f"storage:{fine_account}:{MONTH}"]
    assert billed("error") - error_before == 1
    assert (
        await env.scalar("SELECT status::text FROM billing.jobs WHERE kind = 'storage.bill'")
        != "done"
    )  # failed once: the runner will retry it

    env.storage_usage.down.clear()
    env.clock.advance(hours=1)  # past the retry back-off
    await env.run_jobs()
    sources = sorted(r.source_id for r in await usage_rows(env))  # type: ignore[attr-defined]
    assert sources == sorted(
        [f"storage:{fine_account}:{MONTH}", f"storage:{broken_account}:{MONTH}"]
    )
    assert await balance(env, wallet_code(fine_account)) == -(int(taxable_of(Paise(99900))) - 847)
