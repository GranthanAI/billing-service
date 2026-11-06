"""user.deleted: end the user's subscriptions, stop every mandate, anonymise the account, and wipe
their Valkey keys.

Kept on purpose: invoices, credit notes, payments, refunds, the ledger and `usage_events`. They are
statutory and financial records (see runbooks/data-retention.md); they carry the user's pseudonymous
id and the GST details the law requires, but no email.
"""

from typing import Literal
from uuid import UUID

import structlog

from billing.app.config import BillingContext
from billing.app.effects import GATEWAY_CANCEL
from billing.app.ports import Counters, UnitOfWork
from billing.app.records import ChangeStatus
from billing.app.transitions import IllegalTransition, apply_transition
from billing.domain.catalog import OwnerType
from billing.domain.subscription import AccountDeleted

log = structlog.get_logger()

CONSUMER = "billing-user"


class UserDeletionService:
    def __init__(self, bctx: BillingContext, counters: Counters) -> None:
        self._b = bctx
        self._counters = counters

    async def apply(self, user_id: UUID, dedupe_key: str) -> Literal["applied", "duplicate"]:
        async with self._b.uow_factory() as uow:
            claimed = await uow.processed.claim(CONSUMER, dedupe_key)
            if claimed:
                await self._end_and_anonymise(uow, user_id, dedupe_key)
        # always, even on a replay: wiping Valkey twice is harmless and heals a crash that
        # happened between the commit and this line
        removed = await self._counters.erase_user(user_id)
        await self._b.invalidate_entitlement(user_id)
        log.info("user_deleted", user_id=str(user_id), valkey_keys=removed, replay=not claimed)
        return "applied" if claimed else "duplicate"

    async def _end_and_anonymise(self, u: UnitOfWork, user_id: UUID, cause: str) -> None:
        account = await u.accounts.get_by_owner(OwnerType.USER, user_id)
        if account is None:
            return
        now = self._b.clock.now()
        for rec in await u.subscriptions.list_open_for_account(account.id):
            locked = await u.subscriptions.lock(rec.id)
            if locked is None:
                continue
            change = await u.changes.open_for_subscription(locked.id)
            if change is not None:  # a mandate waiting for its first charge must not start
                await u.changes.set_status(change.id, ChangeStatus.ABANDONED)
                await u.jobs.enqueue(
                    GATEWAY_CANCEL,
                    {
                        "gateway_subscription_id": change.gateway_subscription_id,
                        "at_cycle_end": False,
                    },
                    now,
                    f"{GATEWAY_CANCEL}:{change.gateway_subscription_id}:0",
                )
            try:
                await apply_transition(u, self._b, locked, AccountDeleted(), cause_ref=cause)
            except IllegalTransition:
                continue
        await u.accounts.anonymise(account.id)
