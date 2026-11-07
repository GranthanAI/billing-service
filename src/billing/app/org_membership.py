"""org.member.joined / org.member.left: keep a member's entitlement right, and tell
notification-service when their own paid plan has become redundant.

Billing never cancels a personal plan (Decision 5): it only emits
`entitlement.personal_plan_redundant` so the user can choose.
"""

from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

import structlog

from billing.app.events import PersonalPlanRedundantEvent
from billing.app.ports import Clock, OrgDirectory, UnitOfWork
from billing.domain.catalog import OwnerType
from billing.domain.subscription import GRANTS_PLAN

log = structlog.get_logger()
CONSUMER = "billing-org"


class OrgMembershipService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        orgs: OrgDirectory,
        invalidate: Callable[[UUID], Awaitable[None]],
        clock: Clock,
    ) -> None:
        self._uow = uow_factory
        self._orgs = orgs
        self._invalidate = invalidate
        self._clock = clock

    async def apply(
        self, *, joined: bool, user_id: UUID, org_id: UUID, dedupe_key: str
    ) -> Literal["applied", "duplicate"]:
        async with self._uow() as uow:
            claimed = await uow.processed.claim(CONSUMER, dedupe_key)
            if claimed and joined:
                await self._maybe_redundant(uow, user_id, org_id)
        # Always, even on a replay: forgetting a cached membership and clearing the entitlement
        # twice is harmless, and it heals a crash between the commit and this point.
        await self._orgs.forget_membership(user_id)
        await self._invalidate(user_id)
        log.info("org_membership_applied", joined=joined, user_id=str(user_id), replay=not claimed)
        return "applied" if claimed else "duplicate"

    async def _maybe_redundant(self, uow: UnitOfWork, user_id: UUID, org_id: UUID) -> None:
        account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
        personal = await uow.subscriptions.live_for_account(account.id) if account else None
        if account is None or personal is None or personal.state.status not in GRANTS_PLAN:
            return
        personal_plan = await uow.plans.by_id(personal.state.plan_version_id)
        org_account = await uow.accounts.get_by_owner(OwnerType.ORG, org_id)
        if personal_plan is None or org_account is None:
            return
        org_ranks: list[tuple[int, str]] = []
        org_sub = await uow.subscriptions.live_for_account(org_account.id)
        if org_sub is not None and org_sub.state.status in GRANTS_PLAN:
            plan = await uow.plans.by_id(org_sub.state.plan_version_id)
            if plan is not None:
                org_ranks.append((plan.plan.rank, plan.plan.plan_code))
        for ov in await uow.overrides.active_for(OwnerType.ORG, org_id, self._clock.now()):
            plan = await uow.plans.by_id(ov.plan_version_id)
            if plan is not None:
                org_ranks.append((plan.plan.rank, plan.plan.plan_code))
        if not org_ranks:
            return
        org_rank, org_code = max(org_ranks)
        if personal_plan.plan.rank <= org_rank:
            await uow.publisher.enqueue(
                PersonalPlanRedundantEvent(
                    user_id=user_id,
                    billing_account_id=account.id,
                    personal_plan_code=personal_plan.plan.plan_code,
                    org_id=org_id,
                    org_plan_code=org_code,
                )
            )
