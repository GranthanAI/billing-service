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


def make_valkey(url: str) -> valkey_asyncio.Valkey:
    return valkey_asyncio.from_url(url, decode_responses=True)


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
