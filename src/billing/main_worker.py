"""Worker entry point: consumers, outbox relay and job runner arrive in Phases 4 onward."""

import asyncio

from billing.observability import configure_logging


async def run() -> None:
    configure_logging()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
