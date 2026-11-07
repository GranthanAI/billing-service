"""StorageUsage: how many bytes a user stores, asked of file-service (contracts/file-service.md).

`GET {FILE_SERVICE_URL}/internal/storage/usage?user_id=` with the bearer service token answers
`{"user_id": "...", "bytes_used": 0, "measured_at": "<ISO>"}`. 5 s timeout; 3 retries on timeouts,
connection errors, 429 and 5xx; any other 4xx fails at once.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx

from billing.app.errors import BillingError

TIMEOUT_SECONDS = 5.0
RETRIES = 3
BACKOFF_SECONDS = 0.2


class StorageUsageUnavailable(BillingError):
    code = "STORAGE_USAGE_UNAVAILABLE"
    title = "file-service could not be asked"


class HttpStorageUsage:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    async def bytes_used(self, user_id: UUID) -> int:
        last = "no attempt made"
        for attempt in range(RETRIES + 1):
            if attempt:
                await self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
            try:
                resp = await self._http.get(
                    "/internal/storage/usage", params={"user_id": str(user_id)}
                )
            except httpx.TransportError as exc:  # includes timeouts
                last = type(exc).__name__
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise StorageUsageUnavailable(f"file-service answered HTTP {resp.status_code}")
            body: dict[str, Any] = resp.json()
            used = int(body["bytes_used"])
            if used < 0:
                raise StorageUsageUnavailable("file-service reported negative bytes_used")
            return used
        raise StorageUsageUnavailable(f"file-service unavailable after retries: {last}")


class FakeStorageUsage:
    """Scripted usage for tests."""

    def __init__(self) -> None:
        self.used: dict[UUID, int] = {}
        self.down: set[UUID] = set()
        self.calls: list[UUID] = []

    async def bytes_used(self, user_id: UUID) -> int:
        self.calls.append(user_id)
        if user_id in self.down:
            raise StorageUsageUnavailable("file-service is down")
        return self.used.get(user_id, 0)
