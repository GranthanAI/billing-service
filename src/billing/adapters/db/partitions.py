import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure(conn: AsyncConnection, months_ahead: int = 2) -> int:
    """Create usage_events partitions for the current IST month and `months_ahead` more.

    Returns how many were created. The caller commits. Phase 8 schedules this daily.
    """
    result = await conn.execute(
        sa.text("SELECT billing.ensure_usage_partitions(:n)"), {"n": months_ahead}
    )
    created: int = result.scalar_one()
    return created
