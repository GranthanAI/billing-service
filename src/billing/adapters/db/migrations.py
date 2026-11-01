from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ROOT = Path(__file__).resolve().parents[4]


def expected_head() -> str:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


async def schema_problem(engine: AsyncEngine) -> str | None:
    """None when the database answers and is at the script head, else the reason it is not."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            current = (
                await conn.execute(text("SELECT version_num FROM billing.alembic_version"))
            ).scalar_one_or_none()
    except Exception as exc:
        return f"database unavailable: {type(exc).__name__}"
    head = expected_head()
    if current != head:
        return f"schema is at {current}, expected {head}"
    return None
