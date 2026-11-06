import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from billing.adapters.ids import Uuid7Generator
from billing.app.records import JobStatus
from billing.jobs.runner import REGISTRY, Handler, JobContext, JobRunner, job_handler
from billing.jobs.scheduler import Recurring, Scheduler
from billing.main_worker import run_worker
from billing.observability import JOBS_DEAD
from billing.settings import Settings
from tests.integration.conftest import FreshDb
from tests.integration.kafka_support import wait_until
from tests.support.clock import TestClock

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
IDS = Uuid7Generator()


def ctx_for(db: FreshDb, clock: TestClock) -> JobContext:
    return JobContext(lambda: db.app_uow(), clock, IDS)


async def add_job(db: FreshDb, kind: str, *, max_attempts: int | None = None) -> int:
    async with db.app_uow() as uow:
        await uow.jobs.enqueue(kind, {"n": 1}, NOW, f"d-{uuid4()}")
        job_id: int = (
            await uow.connection.execute(
                sa.text("SELECT max(id) FROM billing.jobs WHERE kind = :k"), {"k": kind}
            )
        ).scalar_one()
        if max_attempts is not None:
            await uow.connection.execute(
                sa.text("UPDATE billing.jobs SET max_attempts = :m WHERE id = :i"),
                {"m": max_attempts, "i": job_id},
            )
    return job_id


async def row(db: FreshDb, job_id: int) -> Any:
    async with db.app_uow() as uow:
        return (
            await uow.connection.execute(
                sa.text("SELECT * FROM billing.jobs WHERE id = :i"), {"i": job_id}
            )
        ).one()


async def test_concurrent_claimers_never_get_the_same_job(fresh_db: FreshDb) -> None:
    async with fresh_db.app_uow() as uow:
        for i in range(100):
            await uow.jobs.enqueue("t10", {"i": i}, NOW, f"d-{i}")

    async def claimer() -> list[int]:
        seen: list[int] = []
        while True:
            async with fresh_db.app_uow() as uow:
                got = await uow.jobs.claim(7, 300, NOW)
            if not got:
                return seen
            seen += [j.id for j in got]
            await asyncio.sleep(0)

    a, b, c = await asyncio.gather(claimer(), claimer(), claimer())
    everything = a + b + c
    assert len(everything) == 100 and len(set(everything)) == 100


async def test_failing_job_backs_off_then_dies_and_counts(fresh_db: FreshDb) -> None:
    kind = f"t11-{uuid4().hex[:6]}"
    clock = TestClock(NOW)

    async def boom(payload: dict[str, Any], ctx: JobContext) -> None:
        raise RuntimeError("always fails")

    runner = JobRunner(ctx_for(fresh_db, clock), registry={kind: boom})
    job_id = await add_job(fresh_db, kind, max_attempts=3)
    before = JOBS_DEAD.labels(kind=kind)._value.get()  # type: ignore[attr-defined]

    run_ats = []
    for _ in range(3):
        assert await runner.claim_and_start() == 1
        await asyncio.gather(*runner._running)
        r = await row(fresh_db, job_id)
        run_ats.append(r.run_at)
        if r.status == "dead":
            break
        assert r.status == "queued" and r.last_error == "always fails"
        clock.set(r.run_at)  # time passes until the job is due again
    assert (await row(fresh_db, job_id)).status == "dead"
    assert run_ats[0] - NOW == timedelta(seconds=2)  # 2**1
    assert run_ats[1] - run_ats[0] == timedelta(seconds=4)  # 2**2: growing
    assert JOBS_DEAD.labels(kind=kind)._value.get() == before + 1  # type: ignore[attr-defined]
    assert await runner.claim_and_start() == 0


async def test_reaper_requeues_a_job_whose_runner_died(fresh_db: FreshDb) -> None:
    clock = TestClock(NOW)
    job_id = await add_job(fresh_db, "t12")
    async with fresh_db.app_uow() as uow:
        assert len(await uow.jobs.claim(1, 300, NOW)) == 1  # the runner takes it, then is killed
    assert (await row(fresh_db, job_id)).status == "running"
    runner = JobRunner(ctx_for(fresh_db, clock), registry={})
    assert await runner.reap() == 0  # lock still valid
    clock.set(NOW + timedelta(seconds=301))
    assert await runner.reap() == 1
    r = await row(fresh_db, job_id)
    assert r.status == "queued" and r.locked_until is None and r.attempts == 1


async def test_two_schedulers_enqueue_a_slot_once(fresh_db: FreshDb) -> None:
    clock = TestClock(NOW)
    schedule = [Recurring("t13", daily_at="04:00 IST")]
    a = Scheduler(lambda: fresh_db.app_uow(), clock, schedule)
    b = Scheduler(lambda: fresh_db.app_uow(), clock, schedule)
    assert sorted(await asyncio.gather(a.tick(), b.tick())) == [0, 1]
    assert await a.tick() == 0
    clock.set(NOW + timedelta(days=1))  # the next IST day is a new slot
    assert await b.tick() == 1


async def test_unknown_kind_fails_with_no_handler(fresh_db: FreshDb) -> None:
    clock = TestClock(NOW)
    job_id = await add_job(fresh_db, "t15-unknown")
    runner = JobRunner(ctx_for(fresh_db, clock), registry={})
    await runner.claim_and_start()
    await asyncio.gather(*runner._running)
    r = await row(fresh_db, job_id)
    assert r.status == JobStatus.QUEUED and "no handler" in r.last_error


async def test_handler_timeout_fails_the_job(fresh_db: FreshDb) -> None:
    clock = TestClock(NOW)

    async def slow(payload: dict[str, Any], ctx: JobContext) -> None:
        await asyncio.sleep(10)

    job_id = await add_job(fresh_db, "t-slow")
    runner = JobRunner(ctx_for(fresh_db, clock), handler_timeout=0.1, registry={"t-slow": slow})
    await runner.claim_and_start()
    await asyncio.gather(*runner._running)
    r = await row(fresh_db, job_id)
    assert r.status == "queued" and r.last_error == "TimeoutError"


async def test_runner_loop_runs_a_job_to_done(fresh_db: FreshDb) -> None:
    clock = TestClock(NOW)
    done: list[int] = []

    async def ok(payload: dict[str, Any], ctx: JobContext) -> None:
        done.append(payload["n"])

    job_id = await add_job(fresh_db, "t-ok")
    runner = JobRunner(
        ctx_for(fresh_db, clock), poll_interval=0.05, reap_every=0.1, registry={"t-ok": ok}
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    try:
        await wait_until(lambda: bool(done), 10)
        await asyncio.sleep(0.3)
    finally:
        stop.set()
        await task
    assert (await row(fresh_db, job_id)).status == "done"


# --- worker -------------------------------------------------------------------------------------

SLOW_STARTED: list[str] = []


@job_handler("test.slow-sigterm")
async def slow_sigterm(payload: dict[str, Any], ctx: JobContext) -> None:
    SLOW_STARTED.append("started")
    await asyncio.sleep(2)
    async with ctx.uow_factory() as uow:
        await uow.audit.record(UUID(int=1), "slow.finished", "job", "x", "t", None, None)


async def test_stop_signal_lets_a_running_job_finish_and_exits_cleanly(
    fresh_db: FreshDb, kafka: str
) -> None:
    REGISTRY_BEFORE: dict[str, Handler] = dict(REGISTRY)
    assert "test.slow-sigterm" in REGISTRY_BEFORE
    settings = Settings(
        billing_database_url=fresh_db.app_url,
        billing_migration_database_url=fresh_db.urls.migrator_for(fresh_db.name),
        billing_valkey_url="redis://unused",
        kafka_bootstrap_servers=kafka,
        razorpay_key_id="k",
        razorpay_key_secret="s",
        razorpay_webhook_secret="w",
        gateway_shared_secret="g",
        service_token_keys="t",
        supplier_state_code="10",
        job_concurrency=2,
    )
    job_id = await add_job(fresh_db, "test.slow-sigterm")
    # the job is due "now" for the worker, which uses the real clock
    async with fresh_db.app_uow() as uow:
        await uow.connection.execute(
            sa.text("UPDATE billing.jobs SET run_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": job_id},
        )
    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(settings, stop, health_port=None, usage_consumer=False, user_consumer=False)
    )
    try:
        await wait_until(lambda: bool(SLOW_STARTED), 30)
        stop.set()  # what SIGTERM does
        await asyncio.wait_for(worker, 30)  # exits cleanly, within the 30 s drain window
    finally:
        stop.set()
        if not worker.done():
            worker.cancel()
    assert (await row(fresh_db, job_id)).status == "done"
    async with fresh_db.app_uow() as uow:
        finished = (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.admin_audit WHERE action = 'slow.finished'")
            )
        ).scalar_one()
        # run_worker ran partitions.ensure at startup and the scheduler enqueued the daily job
        scheduled = (
            await uow.connection.execute(
                sa.text("SELECT count(*) FROM billing.jobs WHERE kind = 'partitions.ensure'")
            )
        ).scalar_one()
    assert finished == 1 and scheduled == 1
