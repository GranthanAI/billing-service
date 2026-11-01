import asyncio
import os

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
SCHEMA = "billing"


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get(
        "BILLING_MIGRATION_DATABASE_URL", ""
    )
    if not url:
        raise RuntimeError("set BILLING_MIGRATION_DATABASE_URL (the billing_migrator role)")
    return url


def _run(connection: Connection) -> None:
    # The version table lives in the billing schema, so the schema must exist first.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    connection.execute(text(f"SET search_path = {SCHEMA}"))
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema=SCHEMA,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline mode is not supported")
asyncio.run(_run_async())
