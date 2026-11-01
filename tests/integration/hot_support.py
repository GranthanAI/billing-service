"""A real ReserveService over the hot database and Valkey, plus helpers to make users."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio

from billing.adapters.clock import SystemClock
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.adapters.valkey.counters import ValkeyCounters
from billing.adapters.valkey.entitlement_cache import ValkeyEntitlementCache
from billing.app.entitlements import EntitlementService, ModelCatalog
from billing.app.ports import Clock
from billing.app.records import OverrideRecord
from billing.app.reserve import CheckAndReserveRequest, ReserveService
from billing.domain.catalog import ModelGroup, OwnerType, ReserveKind
from tests.integration.conftest import FreshDb


@dataclass
class Stack:
    reserve: ReserveService
    entitlements: EntitlementService
    counters: ValkeyCounters
    hot: FreshDb
    vk: valkey_asyncio.Valkey
    clock: Clock
    uow_calls: list[int]  # one entry per Postgres unit of work the services opened


def make_stack(hot: FreshDb, vk: valkey_asyncio.Valkey, clock: Clock | None = None) -> Stack:
    clock = clock or SystemClock()
    ids = Uuid7Generator()
    calls: list[int] = []

    def uow_factory() -> SqlUnitOfWork:
        calls.append(1)
        return SqlUnitOfWork(hot.app_engine, ids, clock)

    counters = ValkeyCounters(vk)
    ent = EntitlementService(uow_factory, ValkeyEntitlementCache(vk), clock)
    reserve = ReserveService(ent, ModelCatalog(uow_factory, clock), counters, clock, ids)
    return Stack(reserve, ent, counters, hot, vk, clock, calls)


async def grant_plan(hot: FreshDb, user_id: UUID, plan_code: str) -> None:
    """An override that makes `user_id` a Pro/Max user, the way staff would."""
    now = datetime.now(UTC)
    async with hot.app_uow() as uow:
        plan = await uow.plans.current(plan_code, None)
        assert plan is not None
        await uow.overrides.insert(
            OverrideRecord(
                id=uuid4(),
                owner_type=OwnerType.USER,
                owner_id=user_id,
                plan_version_id=plan.plan.id,
                reason="test",
                starts_at=now - timedelta(hours=1),
                expires_at=now + timedelta(days=30),
                granted_by=uuid4(),
            )
        )


async def make_user(hot: FreshDb, plan: str | None = None, *, blocked: bool = False) -> UUID:
    user_id = uuid4()
    if plan is not None:
        await grant_plan(hot, user_id, plan)
    if blocked:
        async with hot.app_uow() as uow:
            await uow.accounts.get_or_create(OwnerType.USER, user_id, "b@example.com")
        async with hot.migrator_uow() as uow:
            await uow.connection.execute(
                sa.text(
                    "UPDATE billing.billing_accounts SET blocked_at = now(), "
                    "blocked_reason = 'test' WHERE owner_id = :u"
                ),
                {"u": user_id},
            )
    return user_id


def msg(
    user_id: UUID,
    key: str | None = None,
    group: ModelGroup | None = ModelGroup.FAST,
    model: str = "",
    kind: ReserveKind = ReserveKind.MESSAGE,
) -> CheckAndReserveRequest:
    return CheckAndReserveRequest(
        user_id=user_id,
        idempotency_key=key or str(uuid4()),
        kind=kind,
        requested_group=group,
        requested_model=model,
    )


Spy = Callable[[], int]
