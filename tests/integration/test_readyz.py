import httpx
import sqlalchemy as sa
import valkey.asyncio as valkey_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.db.migrations import expected_head
from billing.adapters.valkey.client import make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.main_api import app


async def _get(
    engine: AsyncEngine | None, client: valkey_asyncio.Valkey | None = None
) -> httpx.Response:
    app.state.engine = engine
    app.state.valkey = client
    app.state.counters = ValkeyCounters(client) if client is not None else None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get("/readyz")


async def test_ready_after_migrations(app_engine: AsyncEngine, vk: valkey_asyncio.Valkey) -> None:
    r = await _get(app_engine, vk)
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


async def test_not_ready_when_schema_is_behind(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, vk: valkey_asyncio.Valkey
) -> None:
    async with migrator_engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE billing.alembic_version SET version_num = '0006_plumbing'")
        )
    try:
        r = await _get(app_engine, vk)
        assert r.status_code == 503
        assert "0006_plumbing" in r.json()["reason"]
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE billing.alembic_version SET version_num = :head"),
                {"head": expected_head()},
            )


async def test_not_ready_without_database() -> None:
    r = await _get(None)
    assert r.status_code == 503


async def test_not_ready_without_valkey(app_engine: AsyncEngine) -> None:
    r = await _get(app_engine, None)
    assert r.status_code == 503 and "valkey" in r.json()["reason"]


async def test_not_ready_when_valkey_could_evict_counters(
    app_engine: AsyncEngine, valkey_lru_url: str
) -> None:
    client = make_valkey(valkey_lru_url)
    try:
        r = await _get(app_engine, client)
    finally:
        await client.aclose()
    assert r.status_code == 503
    assert "allkeys-lru" in r.json()["reason"] and "noeviction" in r.json()["reason"]
