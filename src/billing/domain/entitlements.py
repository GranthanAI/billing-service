"""Pick the winning grant for a user."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from billing.domain.catalog import Limits, ModelGroup, OrgVariant, PlanVersion

_NEVER = datetime.max.replace(tzinfo=UTC)


class GrantSource(StrEnum):
    OVERRIDE = "override"
    ORG = "org"
    SUBSCRIPTION = "subscription"
    PAYG = "payg"
    FREE = "free"


PAID_PRIORITY = {GrantSource.ORG: 2, GrantSource.SUBSCRIPTION: 1}


@dataclass(frozen=True, slots=True)
class Grant:
    source: GrantSource
    plan_version: PlanVersion
    billing_account_id: UUID | None = None
    subscription_id: UUID | None = None
    variant: OrgVariant | None = None
    expires_at: datetime | None = None


def pick(grants: Sequence[Grant]) -> Grant:
    if not grants:
        raise ValueError("no grants")
    overrides = [g for g in grants if g.source is GrantSource.OVERRIDE]
    if overrides:
        return max(overrides, key=lambda g: (g.plan_version.rank, g.expires_at or _NEVER))
    paid = [g for g in grants if g.source in PAID_PRIORITY]
    if paid:
        return max(paid, key=lambda g: (g.plan_version.rank, PAID_PRIORITY[g.source]))
    payg = [g for g in grants if g.source is GrantSource.PAYG]
    if payg:
        return payg[0]
    free = [g for g in grants if g.source is GrantSource.FREE]
    if not free:
        raise ValueError("no free grant present")
    return free[0]


@dataclass(frozen=True, slots=True)
class Entitlement:
    grant: Grant
    limits: Limits
    allowed_groups: frozenset[ModelGroup]
    extra_models: frozenset[str]
    overflow_wallet_account_id: UUID | None
    version_hash: str

    @staticmethod
    def of(grant: Grant, overflow_wallet_account_id: UUID | None = None) -> "Entitlement":
        pv = grant.plan_version
        payload = {
            "plan_code": pv.plan_code,
            "version": pv.version,
            "variant": grant.variant.value if grant.variant else None,
            "source": grant.source.value,
            "billing_account_id": (
                str(grant.billing_account_id) if grant.billing_account_id else None
            ),
            "limits": asdict(pv.limits),
            "groups": sorted(g.value for g in pv.allowed_groups),
            "extra_models": sorted(pv.extra_models),
            "wallet": str(overflow_wallet_account_id) if overflow_wallet_account_id else None,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return Entitlement(
            grant=grant,
            limits=pv.limits,
            allowed_groups=pv.allowed_groups,
            extra_models=pv.extra_models,
            overflow_wallet_account_id=overflow_wallet_account_id,
            version_hash=digest,
        )
