"""Who may do what right now: the winning grant for a user, cached in Valkey."""

import asyncio
import time
from collections.abc import Callable
from uuid import UUID

import structlog

from billing.app.ports import Clock, EntitlementCache, OrgDirectory, UnitOfWork
from billing.app.records import Membership
from billing.domain.catalog import ModelGroup, OwnerType
from billing.domain.entitlements import Entitlement, Grant, GrantSource, pick
from billing.domain.subscription import GRANTS_PLAN

log = structlog.get_logger()
ENTITLEMENT_TTL_SECONDS = 600
DEGRADED_TTL_SECONDS = 30  # org-service was unreachable: look again soon
L1_MAX_ENTRIES = 50_000


class EntitlementService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        cache: EntitlementCache,
        clock: Clock,
        ttl_seconds: int = ENTITLEMENT_TTL_SECONDS,
        l1_ttl_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        orgs: OrgDirectory | None = None,
    ) -> None:
        self._uow = uow_factory
        self._cache = cache
        self._orgs = orgs
        self._clock = clock
        self._ttl = ttl_seconds
        # A few seconds of in-process memory in front of Valkey saves a round trip and a JSON
        # decode on almost every call. Another replica sees an invalidation after at most this
        # long; this process sees it at once. 0 turns it off.
        self._l1_ttl = l1_ttl_seconds
        self._monotonic = monotonic
        self._l1: dict[UUID, tuple[float, Entitlement]] = {}

    async def resolve(self, user_id: UUID) -> Entitlement:
        now = self._monotonic()
        hit = self._l1.get(user_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        ent = await self._cache.get(user_id)
        if ent is None:
            ent, degraded = await self._load(user_id)
            await self._cache.set(
                user_id, ent, min(self._ttl, DEGRADED_TTL_SECONDS) if degraded else self._ttl
            )
        if self._l1_ttl > 0:
            if len(self._l1) >= L1_MAX_ENTRIES:
                self._l1.clear()
            self._l1[user_id] = (now + self._l1_ttl, ent)
        return ent

    async def invalidate(self, user_id: UUID) -> None:
        """Call after anything that changes what a user may do: a committed subscription change,
        an override granted or ended, an account blocked or unblocked, an org membership change."""
        self._l1.pop(user_id, None)
        await self._cache.delete(user_id)

    async def listen(self, ready: asyncio.Event | None = None) -> None:
        """Drop this process's L1 copy whenever any replica invalidates a user. Runs until
        cancelled. Without it the 5 s L1 TTL is the only bound on staleness."""
        async for user_id in self._cache.invalidations():
            if user_id is None:
                if ready is not None:
                    ready.set()
                self._l1.clear()  # we may have missed messages while disconnected
            else:
                self._l1.pop(user_id, None)

    async def invalidate_org(self, org_id: UUID) -> int:
        """An organisation's plan changed: clear every member's entitlement. Returns how many.
        Needs org-service to list the members; if it cannot, the entitlements expire on their own
        within the cache TTL."""
        if self._orgs is None:
            return 0
        try:
            members = await self._orgs.members(org_id)
        except Exception:
            log.warning("org_invalidate_failed", org_id=str(org_id), exc_info=True)
            return 0
        for member in members:
            await self.invalidate(member)
        return len(members)

    async def _membership(self, user_id: UUID) -> tuple[Membership | None, bool]:
        """(the user's active membership, whether org-service could not be asked)."""
        if self._orgs is None:
            return None, False
        try:
            found = await self._orgs.membership(user_id)  # outside any transaction
        except Exception:
            log.warning("org_directory_unavailable", user_id=str(user_id), exc_info=True)
            return None, True
        return (found if found is not None and found.status == "active" else None), False

    async def _load(self, user_id: UUID) -> tuple[Entitlement, bool]:
        """Read-only: the hot path never writes to Postgres. A free user may have no account.
        The bool says org-service was unreachable, so the result should not be cached for long."""
        now = self._clock.now()
        member, degraded = await self._membership(user_id)
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
            if member is not None:
                org_account = await uow.accounts.get_by_owner(OwnerType.ORG, member.org_id)
                org_account_id = org_account.id if org_account else None
                for ov in await uow.overrides.active_for(OwnerType.ORG, member.org_id, now):
                    plan = await uow.plans.by_id(ov.plan_version_id)
                    if plan is not None:
                        grants.append(
                            Grant(
                                GrantSource.OVERRIDE,
                                plan.plan,
                                billing_account_id=org_account_id,  # usage is the org's
                                expires_at=ov.expires_at,
                            )
                        )
                org_sub = (
                    await uow.subscriptions.live_for_account(org_account.id)
                    if org_account
                    else None
                )
                if org_sub is not None and org_sub.state.status in GRANTS_PLAN:
                    plan = await uow.plans.by_id(org_sub.state.plan_version_id)
                    if plan is not None and org_account is not None:
                        grants.append(
                            Grant(
                                GrantSource.ORG,
                                plan.plan,
                                billing_account_id=org_account.id,
                                subscription_id=org_sub.id,
                                variant=member.org_type,
                            )
                        )
            # v3: the PAYG wallet becomes overflow_wallet_account_id below.
            blocked = bool(account and account.is_blocked)
        return (
            Entitlement.of(pick(grants), overflow_wallet_account_id=None, account_blocked=blocked),
            degraded,
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
