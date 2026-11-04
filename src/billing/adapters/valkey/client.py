"""Valkey connection and the startup checks that protect the counters."""

import inspect
from collections.abc import Awaitable

import structlog
import valkey.asyncio as valkey_asyncio
from valkey.exceptions import ResponseError

log = structlog.get_logger()


async def aw[T](value: Awaitable[T] | T) -> T:
    """valkey-py types sync and async calls as one union; this narrows it for mypy."""
    if inspect.isawaitable(value):
        return await value
    return value


POOL_SIZE = 64


def make_valkey(url: str, max_connections: int = POOL_SIZE) -> valkey_asyncio.Valkey:
    """A client over a bounded, blocking pool.

    An unbounded pool opens a new connection for every concurrent request, so a burst of 200
    reserves means 200 simultaneous handshakes; a bounded one reuses connections and makes the
    excess wait its turn (up to `timeout` seconds) instead of storming the server.
    """
    pool = valkey_asyncio.BlockingConnectionPool.from_url(
        url, max_connections=max_connections, timeout=5.0, decode_responses=True
    )
    return valkey_asyncio.Valkey(connection_pool=pool)


async def config_problem(client: valkey_asyncio.Valkey) -> str | None:
    """Why this instance is unsafe for counters, or None.

    An evicted counter hands out free messages, so the policy must be noeviction and AOF on. A
    managed Valkey may refuse CONFIG; then we cannot check and only warn.
    """
    try:
        policy = (await client.config_get("maxmemory-policy")).get("maxmemory-policy")
        aof = (await client.config_get("appendonly")).get("appendonly")
    except ResponseError as exc:
        log.warning("valkey_config_unreadable", error=str(exc))
        return None
    if policy != "noeviction":
        return f"valkey maxmemory-policy is {policy!r}, expected 'noeviction'"
    if aof != "yes":
        return "valkey appendonly is off; counters would be lost on restart"
    return None
