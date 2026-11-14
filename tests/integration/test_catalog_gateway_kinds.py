"""G1: catalog sync heals gateway plan ids that belong to another gateway or no longer exist, and never touches good ones.
Runs against a throwaway database (shared Postgres via TEST_PG_URL)."""

from uuid import uuid4

from billing.app.catalog import sync_catalog
from billing.adapters.ids import Uuid7Generator
from tests.integration.conftest import FreshDb
from tests.integration.test_catalog import NOW, TEXT, seed, spec_of, versions
from tests.support.clock import TestClock
from tests.support.fake_gateway import FakeGateway


class SimGateway(FakeGateway):
    """Looks like the simulator to the catalog: its own kind and its own id format."""

    kind = "simulator"

    async def ensure_plan(self, spec):  # type: ignore[no-untyped-def]
        before = len(self.plans)
        plan_id = await super().ensure_plan(spec)
        if len(self.plans) > before:                                   # newly created: rename plan_fake_NNNN to plan_SIMNNNN
            new = "plan_SIM" + uuid4().hex[:11]                         # random, like the real simulator's ids
            self.plans[new] = self.plans.pop(plan_id)
            return new
        return plan_id


async def sync(db: FreshDb, gw, *, dry_run: bool = False):  # type: ignore[no-untyped-def]
    return await sync_catalog(spec_of(TEXT), db.migrator_uow, gw, TestClock(NOW), Uuid7Generator(), dry_run=dry_run)


async def pro_row(db: FreshDb):  # type: ignore[no-untyped-def]
    return (await versions(db, "pro"))[0]


async def test_foreign_ids_are_replaced_and_the_kind_is_recorded(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    fake = FakeGateway()
    await sync(fresh_db, fake)
    before = await pro_row(fresh_db)
    assert before.razorpay_plan_month.startswith("plan_fake_") and before.gateway_kind == "fake"

    sim = SimGateway()
    dry = await sync(fresh_db, sim, dry_run=True)                       # a dry run reports but writes nothing
    assert dry.gateway_plans_created == 4 and (await pro_row(fresh_db)).razorpay_plan_month == before.razorpay_plan_month

    report = await sync(fresh_db, sim)
    assert report.gateway_plans_created == 4
    lines = "\n".join(report.lines())
    assert "foreign id plan_fake_" in lines and "(simulator)" in lines
    after = await pro_row(fresh_db)
    assert after.razorpay_plan_month.startswith("plan_SIM") and after.razorpay_plan_year.startswith("plan_SIM")
    assert after.razorpay_plan_month != before.razorpay_plan_month and after.gateway_kind == "simulator"
    assert len(sim.plans) == 4


async def test_ids_of_the_current_gateway_that_exist_are_left_alone(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    sim = SimGateway()
    await sync(fresh_db, sim)
    first = await pro_row(fresh_db)
    again = await sync(fresh_db, sim)
    assert again.gateway_plans_created == 0 and not any(o.gateway_notes for o in again.outcomes)
    second = await pro_row(fresh_db)
    assert (second.razorpay_plan_month, second.razorpay_plan_year) == (first.razorpay_plan_month, first.razorpay_plan_year)
    assert len(sim.plans) == 4                                          # nothing was created a second time


async def test_an_id_the_gateway_no_longer_has_is_recreated(fresh_db: FreshDb) -> None:
    await seed(fresh_db)
    await sync(fresh_db, SimGateway())
    old = await pro_row(fresh_db)
    emptied = SimGateway()                                              # same kind, but the gateway lost its plans
    report = await sync(fresh_db, emptied)
    assert report.gateway_plans_created == 4 and "gone id plan_SIM" in "\n".join(report.lines())
    new = await pro_row(fresh_db)
    assert new.gateway_kind == "simulator" and new.razorpay_plan_month in emptied.plans
    assert (new.razorpay_plan_month, new.razorpay_plan_year) != (old.razorpay_plan_month, old.razorpay_plan_year)


async def test_rows_without_a_recorded_kind_are_judged_by_the_id_prefix(fresh_db: FreshDb) -> None:
    import sqlalchemy as sa

    await seed(fresh_db)
    sim = SimGateway()
    await sync(fresh_db, sim)
    async with fresh_db.migrator_uow() as uow:                           # as after the migration, before any backfill
        await uow.connection.execute(sa.text("UPDATE billing.plan_versions SET gateway_kind = NULL"))
    report = await sync(fresh_db, sim)
    assert report.gateway_plans_created == 0                             # plan_SIM ids are recognised as the simulator's
    assert (await pro_row(fresh_db)).razorpay_plan_month.startswith("plan_SIM")
