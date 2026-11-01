"""Worker entry point: consumers, outbox relay and job runner arrive in Phase 4 onward."""

import asyncio

from billing.adapters.db import partitions
from billing.adapters.db.engine import make_engine
from billing.observability import configure_logging
from billing.settings import get_settings


async def run() -> None:
    configure_logging()
    settings = get_settings()
    engine = make_engine(settings.billing_database_url, pool_size=settings.db_pool_size)
    # Phase 8 schedules this daily; until then, once at startup.
    async with engine.begin() as conn:
        await partitions.ensure(conn, months_ahead=2)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
