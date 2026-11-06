"""A cached "can we reach Kafka" probe for /readyz."""

import contextlib
import time
from collections.abc import Callable

from aiokafka.admin import AIOKafkaAdminClient


class KafkaProbe:
    """Returns None when the brokers answer, else a reason. Successes are cached for 15 s and
    failures for 5 s, so a readiness poll never opens a connection per request."""

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        ok_ttl: float = 15.0,
        fail_ttl: float = 5.0,
        timeout_ms: int = 2000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._servers = bootstrap_servers
        self._ok_ttl, self._fail_ttl = ok_ttl, fail_ttl
        self._timeout_ms = timeout_ms
        self._monotonic = monotonic
        self._until = float("-inf")
        self._last: str | None = None

    async def __call__(self) -> str | None:
        now = self._monotonic()
        if now < self._until:
            return self._last
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._servers,
            request_timeout_ms=self._timeout_ms,
            client_id="billing-readyz",
        )
        try:
            await admin.start()
            await admin.list_topics()
            self._last = None
        except Exception as exc:
            self._last = f"kafka unavailable: {type(exc).__name__}"
        finally:
            with contextlib.suppress(Exception):  # closing a client that never connected
                await admin.close()
        self._until = self._monotonic() + (self._ok_ttl if self._last is None else self._fail_ttl)
        return self._last
