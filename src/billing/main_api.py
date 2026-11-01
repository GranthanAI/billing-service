import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.catalog.yaml_loader import CatalogError, load_catalog
from billing.adapters.db.engine import make_engine
from billing.adapters.db.migrations import schema_problem
from billing.api.rest.public import router as public_router
from billing.observability import configure_logging, metrics_payload
from billing.settings import get_settings

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Tests (and Phase 5 wiring) may pre-set app.state.engine; otherwise build it from settings.
    owned: AsyncEngine | None = None
    if getattr(app.state, "engine", None) is None:
        try:
            settings = get_settings()
        except Exception:
            app.state.engine = None  # /readyz reports 503 until the service is configured
        else:
            owned = make_engine(settings.billing_database_url, pool_size=settings.db_pool_size)
            app.state.engine = owned
            app.state.payg_enabled = settings.payg_enabled
            try:
                app.state.catalog_version = load_catalog(settings.plans_file).catalog_version
            except CatalogError:
                app.state.catalog_version = ""
    yield
    if owned is not None:
        await owned.dispose()


app = FastAPI(title="billing-api", lifespan=lifespan)
app.include_router(public_router)


@app.middleware("http")
async def correlation_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    cid = request.headers.get("X-Correlation-ID") or f"c-{uuid.uuid4().hex[:12]}"
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        return JSONResponse({"status": "not_ready", "reason": "database not configured"}, 503)
    # Valkey, Lua scripts and the catalog checksum are added in later phases.
    problem = await schema_problem(engine)
    if problem:
        return JSONResponse({"status": "not_ready", "reason": problem}, 503)
    return JSONResponse({"status": "ready"})


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)
