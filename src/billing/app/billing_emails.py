"""Enqueueing transactional emails (job `email.send`) inside the transaction of the change.

Nothing is sent here, and never from an API handler: the worker's email.send handler renders and
sends. The dedupe key is "<template>:<entity id>", so replaying the same webhook or job adds
nothing. The payload holds only ids and plain facts (no addresses, no links with tokens).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import structlog

from billing.app.config import BillingConfig
from billing.app.ports import UnitOfWork
from billing.app.records import BillingAccount
from billing.observability import EMAIL_SKIPPED

log = structlog.get_logger()

EMAIL_JOB = "email.send"
EMAIL_MAX_ATTEMPTS = 5
TEMPLATES = (
    "receipt",
    "payment_failed",
    "subscription_suspended",
    "renewal_upcoming",
    "subscription_cancelled",
    "refund_issued",
    "credits_expiring",
)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def enqueue_email(
    uow: UnitOfWork,
    config: BillingConfig,
    template: str,
    entity_id: str,
    account: BillingAccount | None,
    data: Mapping[str, Any],
    now: datetime,
) -> bool:
    """True when a new job was queued. False: disabled, no recipient, or already queued."""
    assert template in TEMPLATES, template
    if not config.email_enabled:
        return False
    if account is None or not account.billing_email:
        EMAIL_SKIPPED.labels(template=template, reason="no_recipient").inc()
        log.info(
            "email_skipped_no_recipient",
            template=template,
            billing_account_id=str(account.id) if account else None,
        )
        return False
    payload = {
        "template": template,
        "key": f"{template}:{entity_id}",  # the Message-ID is derived from it
        "account_id": str(account.id),
        "data": dict(data),
    }
    return await uow.jobs.enqueue(
        EMAIL_JOB, payload, now, f"{template}:{entity_id}", max_attempts=EMAIL_MAX_ATTEMPTS
    )
