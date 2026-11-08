"""entitlements.changed (plan 4.4.7): told through the outbox, in the transaction of the change.

A user's change enqueues the event directly. An organisation's change enqueues the job
`entitlements.fanout` in the same transaction; its handler asks org-service for the members and
enqueues one event per member (org-service is never called inside a transaction).
"""

from datetime import datetime
from uuid import UUID, uuid4

from billing.app.events import EntitlementsChangedEvent
from billing.app.ports import UnitOfWork
from billing.domain.catalog import OwnerType

FANOUT_JOB = "entitlements.fanout"
REASONS = ("subscription", "override", "block", "org", "wallet", "catalog")


async def announce(uow: UnitOfWork, user_id: UUID, reason: str) -> None:
    """One `entitlements.changed` for a user, rolled back with the caller's transaction."""
    assert reason in REASONS, reason
    await uow.publisher.enqueue(EntitlementsChangedEvent(user_id=user_id, reason=reason))


async def announce_owner(
    uow: UnitOfWork, owner_type: OwnerType, owner_id: UUID, reason: str, now: datetime
) -> None:
    """For a user, the event; for an organisation, the fan-out job (one event per member)."""
    if owner_type is OwnerType.USER:
        await announce(uow, owner_id, reason)
        return
    assert reason in REASONS, reason
    await uow.jobs.enqueue(
        FANOUT_JOB,
        {"org_id": str(owner_id), "reason": reason},
        now,
        f"{FANOUT_JOB}:{owner_id}:{uuid4()}",
    )
