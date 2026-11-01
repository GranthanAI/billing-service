"""ent:{uid}: the resolved entitlement as JSON, so a hit needs no Postgres."""

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey import keys
from billing.domain.catalog import Limits, ModelGroup, OrgVariant, PlanVersion
from billing.domain.entitlements import Entitlement, Grant, GrantSource
from billing.domain.money import Paise


def _opt_uuid(v: str | None) -> UUID | None:
    return UUID(v) if v else None


def encode(ent: Entitlement) -> str:
    g, pv = ent.grant, ent.grant.plan_version
    return json.dumps(
        {
            "source": g.source.value,
            "billing_account_id": str(g.billing_account_id) if g.billing_account_id else None,
            "subscription_id": str(g.subscription_id) if g.subscription_id else None,
            "variant": g.variant.value if g.variant else None,
            "expires_at": g.expires_at.isoformat() if g.expires_at else None,
            "plan": {
                "id": str(pv.id),
                "plan_code": pv.plan_code,
                "version": pv.version,
                "rank": pv.rank,
                "variant": pv.variant.value if pv.variant else None,
                "price_month": pv.price_month,
                "price_year": pv.price_year,
                "limits": asdict(pv.limits),
                "allowed_groups": sorted(x.value for x in pv.allowed_groups),
                "extra_models": sorted(pv.extra_models),
            },
            "wallet": str(ent.overflow_wallet_account_id)
            if ent.overflow_wallet_account_id
            else None,
            "version_hash": ent.version_hash,
            "account_blocked": ent.account_blocked,
        },
        separators=(",", ":"),
    )


def decode(raw: str) -> Entitlement:
    d: dict[str, Any] = json.loads(raw)
    p = d["plan"]
    plan = PlanVersion(
        id=UUID(p["id"]),
        plan_code=p["plan_code"],
        version=p["version"],
        rank=p["rank"],
        variant=OrgVariant(p["variant"]) if p["variant"] else None,
        price_month=Paise(p["price_month"]) if p["price_month"] is not None else None,
        price_year=Paise(p["price_year"]) if p["price_year"] is not None else None,
        limits=Limits(**p["limits"]),
        allowed_groups=frozenset(ModelGroup(x) for x in p["allowed_groups"]),
        extra_models=frozenset(p["extra_models"]),
    )
    grant = Grant(
        source=GrantSource(d["source"]),
        plan_version=plan,
        billing_account_id=_opt_uuid(d["billing_account_id"]),
        subscription_id=_opt_uuid(d["subscription_id"]),
        variant=OrgVariant(d["variant"]) if d["variant"] else None,
        expires_at=datetime.fromisoformat(d["expires_at"]) if d["expires_at"] else None,
    )
    return Entitlement(
        grant=grant,
        limits=plan.limits,
        allowed_groups=plan.allowed_groups,
        extra_models=plan.extra_models,
        overflow_wallet_account_id=_opt_uuid(d["wallet"]),
        version_hash=d["version_hash"],
        account_blocked=d["account_blocked"],
    )


class ValkeyEntitlementCache:
    def __init__(self, client: valkey_asyncio.Valkey) -> None:
        self._c = client

    async def get(self, user_id: UUID) -> Entitlement | None:
        raw: str | None = await self._c.get(keys.ent(user_id))
        return decode(raw) if raw else None

    async def set(self, user_id: UUID, entitlement: Entitlement, ttl_seconds: int) -> None:
        await self._c.set(keys.ent(user_id), encode(entitlement), ex=ttl_seconds)

    async def delete(self, user_id: UUID) -> None:
        await self._c.delete(keys.ent(user_id))
