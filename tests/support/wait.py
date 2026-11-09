"""wait_until: poll a condition every 50 ms instead of sleeping a fixed time."""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

POLL_SECONDS = 0.05


async def wait_until(
    condition: Callable[[], Awaitable[bool]] | Callable[[], bool],
    timeout: float = 30.0,
    *,
    interval: float = POLL_SECONDS,
    message: str = "condition not met in time",
) -> None:
    """Returns as soon as `condition()` is true; raises TimeoutError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(message)
        await asyncio.sleep(interval)
