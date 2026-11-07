"""PAYG_ENABLED=false: the wallet jobs do nothing and log that they skipped."""

import structlog

from tests.integration.billing_env import Env
from tests.integration.test_wallet import services, top_up

JOBS = ("credits.expire", "wallet.resync", "wallet.mtd_reset", "storage.bill")


async def test_wallet_jobs_do_nothing_with_the_flag_off_and_work_with_it_on(env: Env) -> None:
    _, _, wallet = services(env)
    await top_up(env, wallet)
    acct = await env.scalar("SELECT id FROM billing.billing_accounts")
    await env.counters.wallet_credit(acct, 777)  # drift that wallet.resync would repair
    env.clock.advance(days=400)  # the lot is past its 12 months
    env.storage_usage.used[env.user] = 50 * 1024**3
    lots_before = await env.scalar("SELECT sum(remaining_paise) FROM billing.credit_lots")
    assert lots_before == 16864

    env.set_payg(False)
    with structlog.testing.capture_logs() as logs:
        for job in JOBS:
            await env.run_job(job)
    skipped = [e["job"] for e in logs if e["event"] == "wallet_job_skipped"]
    assert sorted(skipped) == sorted(JOBS)
    assert await env.scalar("SELECT sum(remaining_paise) FROM billing.credit_lots") == 16864
    assert (await env.counters.wallet_get(acct))["balance"] == 16864 + 777  # not repaired
    assert await env.scalar("SELECT count(*) FROM billing.usage_events WHERE kind = 'storage'") == 0
    assert env.storage_usage.calls == []

    env.set_payg(True)  # the same jobs, flag on: they act
    await env.run_job("wallet.resync")
    assert (await env.counters.wallet_get(acct))["balance"] == 16864
    await env.run_job("credits.expire")
    assert await env.scalar("SELECT sum(remaining_paise) FROM billing.credit_lots") == 0
