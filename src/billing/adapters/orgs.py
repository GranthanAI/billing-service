"""OrgDirectory: what billing needs to know from org-service (membership, org details, members).

The contract (org-service is not built yet; see contracts/org-service.md):
  GET /internal/memberships/{user_id}  -> {org_id, org_type, role, status}, or 404
  GET /internal/orgs/{org_id}          -> {name, type, gstin, billing_email, state_code,
                                           member_spend_cap_paise}, or 404
  GET /internal/orgs/{org_id}/members  -> {user_ids: [...]}
All calls carry `Authorization: Bearer <service token>`.
"""

import json
from typing import Any
from uuid import UUID

import httpx
import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey.client import aw
from billing.app.errors import OrgDirectoryUnavailable
from billing.app.records import Membership, OrgInfo
from billing.domain.catalog import OrgVariant

TIMEOUT_SECONDS = 2.0  # this is on the entitlement cache-miss path
MEMBERS_PAGE = 5000


def _membership(d: dict[str, Any]) -> Membership:
    return Membership(
        org_id=UUID(d["org_id"]),
        org_type=OrgVariant(d["org_type"]),
        role=str(d.get("role", "member")),
        status=str(d.get("status", "active")),
    )


def _org(org_id: UUID, d: dict[str, Any]) -> OrgInfo:
    cap = d.get("member_spend_cap_paise")
    return OrgInfo(
        org_id=org_id,
        name=str(d["name"]),
        type=OrgVariant(d["type"]),
        gstin=d.get("gstin") or None,
        billing_email=d.get("billing_email") or None,
        state_code=d.get("state_code") or None,
        member_spend_cap_paise=int(cap) if cap is not None else None,
    )


class HttpOrgDirectory:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> dict[str, Any] | None:
        last = ""
        for _ in range(2):  # one retry for a dropped connection
            try:
                resp = await self._http.get(path)
            except httpx.TransportError as exc:
                last = type(exc).__name__
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise OrgDirectoryUnavailable(
                    f"org-service refused {path}: HTTP {resp.status_code}"
                )
            body: dict[str, Any] = resp.json()
            return body
        raise OrgDirectoryUnavailable(f"org-service unavailable for {path}: {last}")

    async def membership(self, user_id: UUID) -> Membership | None:
        found = await self._get(f"/internal/memberships/{user_id}")
        return _membership(found) if found else None

    async def org(self, org_id: UUID) -> OrgInfo | None:
        found = await self._get(f"/internal/orgs/{org_id}")
        return _org(org_id, found) if found else None

    async def members(self, org_id: UUID) -> list[UUID]:
        found = await self._get(f"/internal/orgs/{org_id}/members")
        return [UUID(u) for u in (found or {}).get("user_ids", [])]

    async def forget_membership(self, user_id: UUID) -> None:
        return None  # nothing cached here


class CachedOrgDirectory:
    """Memberships and org details cached in Valkey for `ttl` seconds (a "not found" too).

    A membership that changes is announced by org.member.* events; the consumer calls
    `forget_membership` so the entitlement is recomputed from the new truth, not the cache."""

    def __init__(self, inner: Any, client: valkey_asyncio.Valkey, ttl_seconds: int = 60) -> None:
        self._inner = inner
        self._c = client
        self._ttl = ttl_seconds

    @staticmethod
    def _mkey(user_id: UUID) -> str:
        return f"orgdir:m:{{{user_id}}}"

    async def membership(self, user_id: UUID) -> Membership | None:
        key = self._mkey(user_id)
        raw: str | None = await aw(self._c.get(key))
        if raw is not None:
            d = json.loads(raw)
            return _membership(d) if d else None
        found: Membership | None = await self._inner.membership(user_id)
        value = (
            {
                "org_id": str(found.org_id),
                "org_type": found.org_type.value,
                "role": found.role,
                "status": found.status,
            }
            if found
            else {}
        )
        await aw(self._c.set(key, json.dumps(value), ex=self._ttl))
        return found

    async def org(self, org_id: UUID) -> OrgInfo | None:
        key = f"orgdir:o:{{{org_id}}}"
        raw: str | None = await aw(self._c.get(key))
        if raw is not None:
            d = json.loads(raw)
            return _org(org_id, d) if d else None
        found: OrgInfo | None = await self._inner.org(org_id)
        value = (
            {
                "name": found.name,
                "type": found.type.value,
                "gstin": found.gstin,
                "billing_email": found.billing_email,
                "state_code": found.state_code,
                "member_spend_cap_paise": found.member_spend_cap_paise,
            }
            if found
            else {}
        )
        await aw(self._c.set(key, json.dumps(value), ex=self._ttl))
        return found

    async def members(self, org_id: UUID) -> list[UUID]:
        members: list[UUID] = await self._inner.members(org_id)  # never cached: used to invalidate
        return members

    async def forget_membership(self, user_id: UUID) -> None:
        await aw(self._c.delete(self._mkey(user_id)))
        await self._inner.forget_membership(user_id)


class NoOrgDirectory:
    """Organisations switched off (no ORG_SERVICE_URL): nobody is a member of anything."""

    async def membership(self, user_id: UUID) -> Membership | None:
        return None

    async def org(self, org_id: UUID) -> OrgInfo | None:
        return None

    async def members(self, org_id: UUID) -> list[UUID]:
        return []

    async def forget_membership(self, user_id: UUID) -> None:
        return None


class FakeOrgDirectory:
    """In-memory org-service for tests: scripted memberships, org details and member lists."""

    def __init__(self) -> None:
        self.memberships: dict[UUID, Membership] = {}
        self.orgs: dict[UUID, OrgInfo] = {}
        self.calls: list[tuple[str, UUID]] = []
        self.down = False

    def add_org(self, info: OrgInfo) -> None:
        self.orgs[info.org_id] = info

    def join(
        self, user_id: UUID, org_id: UUID, org_type: OrgVariant, *, status: str = "active"
    ) -> None:
        self.memberships[user_id] = Membership(org_id, org_type, "member", status)

    def leave(self, user_id: UUID) -> None:
        self.memberships.pop(user_id, None)

    def _check(self, what: str, key: UUID) -> None:
        self.calls.append((what, key))
        if self.down:
            raise OrgDirectoryUnavailable("org-service is down")

    async def membership(self, user_id: UUID) -> Membership | None:
        self._check("membership", user_id)
        return self.memberships.get(user_id)

    async def org(self, org_id: UUID) -> OrgInfo | None:
        self._check("org", org_id)
        return self.orgs.get(org_id)

    async def members(self, org_id: UUID) -> list[UUID]:
        self._check("members", org_id)
        return [u for u, m in self.memberships.items() if m.org_id == org_id]

    async def forget_membership(self, user_id: UUID) -> None:
        return None
