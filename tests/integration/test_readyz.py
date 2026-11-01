import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.main_api import app


async def _get(engine: AsyncEngine | None) -> httpx.Response:
    app.state.engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/readyz")


async def test_ready_after_migrations(app_engine: AsyncEngine) -> None:
    r = await _get(app_engine)
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


async def test_not_ready_when_schema_is_behind(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    async with migrator_engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE billing.alembic_version SET version_num = '0006_plumbing'")
        )
    try:
        r = await _get(app_engine)
        assert r.status_code == 503
        assert "0006_plumbing" in r.json()["reason"]
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE billing.alembic_version SET version_num = '0007_guards_grants'")
            )


async def test_not_ready_without_database() -> None:
    r = await _get(None)
    assert r.status_code == 503
