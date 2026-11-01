"""Who may do what right now: the winning grant for a user, cached in Valkey."""

import time
from collections.abc import Callable
from uuid import UUID

from billing.app.ports import Clock, EntitlementCache, UnitOfWork
from billing.domain.catalog import ModelGroup, OwnerType
from billing.domain.entitlements import Entitlement, Grant, GrantSource, pick
from billing.domain.subscription import GRANTS_PLAN

ENTITLEMENT_TTL_SECONDS = 600


class EntitlementService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        cache: EntitlementCache,
        clock: Clock,
        ttl_seconds: int = ENTITLEMENT_TTL_SECONDS,
    ) -> None:
        self._uow = uow_factory
        self._cache = cache
        self._clock = clock
        self._ttl = ttl_seconds

    async def resolve(self, user_id: UUID) -> Entitlement:
        if (cached := await self._cache.get(user_id)) is not None:
            return cached
        ent = await self._load(user_id)
        await self._cache.set(user_id, ent, self._ttl)
        return ent

    async def invalidate(self, user_id: UUID) -> None:
        """Call after anything that changes what a user may do: a committed subscription change,
        an override granted or ended, an account blocked or unblocked, an org membership change."""
        await self._cache.delete(user_id)

    async def _load(self, user_id: UUID) -> Entitlement:
        """Read-only: the hot path never writes to Postgres. A free user may have no account."""
        now = self._clock.now()
        async with self._uow() as uow:
            free = await uow.plans.current("free", None)
            if free is None:
                raise RuntimeError("plan catalog is not synced: no free plan")
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            account_id = account.id if account else None
            grants = [Grant(GrantSource.FREE, free.plan, billing_account_id=account_id)]

            for ov in await uow.overrides.active_for(OwnerType.USER, user_id, now):
                plan = await uow.plans.by_id(ov.plan_version_id)
                if plan is not None:
                    grants.append(
                        Grant(
                            GrantSource.OVERRIDE,
                            plan.plan,
                            billing_account_id=account_id,
                            expires_at=ov.expires_at,
                        )
                    )
            if account is not None:
                sub = await uow.subscriptions.live_for_account(account.id)
                if sub is not None and sub.state.status in GRANTS_PLAN:
                    plan = await uow.plans.by_id(sub.state.plan_version_id)
                    if plan is not None:
                        grants.append(
                            Grant(
                                GrantSource.SUBSCRIPTION,
                                plan.plan,
                                billing_account_id=account.id,
                                subscription_id=sub.id,
                            )
                        )
            # v2: organisation grants (membership, org overrides, org subscription) go here.
            # v3: the PAYG wallet becomes overflow_wallet_account_id below.
            blocked = bool(account and account.is_blocked)
        return Entitlement.of(
            pick(grants), overflow_wallet_account_id=None, account_blocked=blocked
        )


class ModelCatalog:
    """model id -> group, from the effective model prices; cached in process for a minute."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._groups: dict[str, ModelGroup] = {}
        self._loaded_at = float("-inf")

    async def _fresh(self) -> dict[str, ModelGroup]:
        if self._monotonic() - self._loaded_at >= self._ttl:
            async with self._uow() as uow:
                self._groups = await uow.pricing.model_groups_at(self._clock.now())
            self._loaded_at = self._monotonic()
        return self._groups

    async def group_of(self, model: str) -> ModelGroup | None:
        return (await self._fresh()).get(model)

    async def exists(self, model: str) -> bool:
        return model in await self._fresh()

    async def models(self) -> dict[str, ModelGroup]:
        return dict(await self._fresh())
