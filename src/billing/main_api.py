import asyncio
import contextlib
import gc
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import grpc
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.catalog.yaml_loader import CatalogError, load_catalog
from billing.adapters.clock import SystemClock
from billing.adapters.db.engine import make_engine
from billing.adapters.db.gateway_mode import ensure_gateway_mode
from billing.adapters.db.migrations import schema_problem
from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.gateways import gateway_mode, make_gateway, sim_config
from billing.adapters.ids import Uuid7Generator
from billing.adapters.kafka.health import KafkaProbe
from billing.adapters.valkey.client import aw, config_problem, make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.adapters.valkey.entitlement_cache import ValkeyEntitlementCache
from billing.api.grpc.internal import start_grpc_server
from billing.api.rest import errors as rest_errors
from billing.api.rest.admin import router as admin_router
from billing.api.rest.billing import router as billing_router
from billing.api.rest.gateway_auth import GatewayAuthMiddleware
from billing.api.rest.idempotency import IdempotencyMiddleware
from billing.api.rest.me import router as me_router
from billing.api.rest.models import router as models_router
from billing.api.rest.orgs import admin_router as org_admin_router
from billing.api.rest.orgs import internal_router as org_internal_router
from billing.api.rest.public import router as public_router
from billing.api.rest.wallet import router as wallet_router
from billing.api.rest.webhooks import router as webhook_router
from billing.app.account_views import AccountViews
from billing.app.admin import AdminService
from billing.app.context import correlation_id_var
from billing.app.entitlements import EntitlementService, ModelCatalog
from billing.app.model_picker import ModelPicker
from billing.app.reserve import ReserveService
from billing.app.settlements import SettlementService
from billing.observability import (
    AI_BUDGET,
    HOLDS_OPEN,
    LOOP_LAG,
    configure_logging,
    init_sentry,
    metrics_payload,
)
from billing.settings import (
    Settings,
    check_simulation_allowed,
    gateway_secrets,
    get_settings,
    payment_mode,
    webhook_secrets,
)
from billing.wiring import (
    config_from_settings,
    install_billing,
    make_context,
    make_org_directory,
    make_storage,
    payg_prices,
)

configure_logging()
log = structlog.get_logger()
HOLDS_SAMPLE_SECONDS = 15


async def _watch_loop_lag() -> None:
    interval = 0.05
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval)
        LOOP_LAG.observe(max(0.0, time.perf_counter() - started - interval))


async def _sample_holds(counters: ValkeyCounters) -> None:
    while True:
        with contextlib.suppress(Exception):
            HOLDS_OPEN.set(await counters.open_holds())
        await asyncio.sleep(HOLDS_SAMPLE_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the wiring from settings unless a test pre-set app.state.engine."""
    owned_engine: AsyncEngine | None = None
    grpc_server: grpc.aio.Server | None = None
    sampler: asyncio.Task[None] | None = None
    invalidation_listener: asyncio.Task[None] | None = None
    lag_watch: asyncio.Task[None] | None = None
    settings: Settings | None = None
    if getattr(app.state, "engine", None) is None:
        try:
            settings = get_settings()
        except Exception:
            app.state.engine = None  # /readyz reports 503 until the service is configured
    if settings is not None:
        check_simulation_allowed(settings.payment_simulation, settings.environment)
        owned_engine = make_engine(settings.billing_database_url, pool_size=settings.db_pool_size)
        app.state.engine = owned_engine
        await ensure_gateway_mode(
            owned_engine,
            gateway_mode(settings),
            allow_switch=settings.allow_gateway_mode_switch,
        )
        app.state.payment_mode = payment_mode(settings)
        app.state.simulation = settings.payment_simulation
        app.state.frontend_origin = settings.frontend_origin
        app.state.payg_enabled = settings.payg_enabled
        app.state.kafka_probe = KafkaProbe(settings.kafka_bootstrap_servers)
        app.state.gateway_secrets = gateway_secrets(settings)
        init_sentry(settings.sentry_dsn, settings.environment)
        AI_BUDGET.set(settings.daily_ai_budget_inr * 1_000_000)
        try:
            app.state.catalog_version = load_catalog(settings.plans_file).catalog_version
        except CatalogError:
            app.state.catalog_version = ""

        client = make_valkey(settings.billing_valkey_url)
        counters = ValkeyCounters(
            client,
            day_ttl=settings.day_counter_ttl_seconds,
            month_ttl=settings.month_counter_ttl_seconds,
        )
        clock, ids = SystemClock(), Uuid7Generator()

        def uow_factory() -> SqlUnitOfWork:
            return SqlUnitOfWork(owned_engine, ids, clock)

        org_directory = make_org_directory(settings, client)
        entitlements = EntitlementService(
            uow_factory,
            ValkeyEntitlementCache(client),
            clock,
            orgs=org_directory,
            l1_ttl_seconds=settings.entitlement_l1_ttl_seconds,
            payg_enabled=settings.payg_enabled,
        )
        gateway = make_gateway(
            settings.payment_gateway,
            settings.environment,
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
            webhook_secret=webhook_secrets(settings),
            clock=clock,
            simulation=settings.payment_simulation,
            engine=owned_engine,
            sim=sim_config(settings),
        )
        storage = make_storage(settings)
        install_billing(
            app,
            make_context(
                config_from_settings(settings),
                uow_factory,
                gateway,
                clock,
                ids,
                entitlements.invalidate,
                entitlements.invalidate_org,
                counters.wallet_credit,
            ),
            views=AccountViews(
                uow_factory,
                entitlements,
                counters,
                storage,
                clock,
                config_from_settings(settings).gst_registered,
            ),
            admin=AdminService(
                uow_factory,
                clock,
                ids,
                SettlementService(uow_factory, gateway, clock),
                entitlements.invalidate,
                org_directory,
            ),
            orgs=org_directory,
            counters=counters,
            org_service_token=settings.org_service_token,
        )
        reserve = ReserveService(
            entitlements,
            ModelCatalog(uow_factory, clock),
            counters,
            clock,
            ids,
            hold_ttl_seconds=settings.hold_ttl_seconds,
            payg=payg_prices(settings),
        )
        app.state.valkey, app.state.counters, app.state.reserve = client, counters, reserve
        app.state.picker = ModelPicker(
            uow_factory, entitlements, counters, clock, payg_enabled=settings.payg_enabled
        )
        with contextlib.suppress(Exception):
            await counters.ensure_scripts_loaded()
        tokens = [t.strip() for t in settings.service_token_keys.split(",")]
        grpc_server, _ = await start_grpc_server(
            reserve, tokens, settings.grpc_port, production=settings.environment == "production"
        )
        invalidation_listener = asyncio.create_task(entitlements.listen())
        sampler = asyncio.create_task(_sample_holds(counters))
        lag_watch = asyncio.create_task(_watch_loop_lag())
        # Long-lived objects are built; keep the cyclic GC from scanning them and from pausing
        # requests (a gen-2 pause shows up directly in the reserve p99).
        gc.collect()
        gc.freeze()
        gc.set_threshold(50_000, 20, 20)
        log.info(
            "billing_api_started",
            grpc_port=settings.grpc_port,
            payment_mode=app.state.payment_mode,
        )
        if settings.payment_simulation:
            log.warning("payment_simulation_on", note="SIMULATED PAYMENTS - no real money moves")
    yield
    for task in (sampler, lag_watch, invalidation_listener):
        if task is not None:
            task.cancel()
    if grpc_server is not None:
        await grpc_server.stop(5)  # 5 s grace for in-flight calls
    if settings is not None:
        close = getattr(app.state.gateway, "aclose", None)
        if close is not None:
            await close()
        await app.state.valkey.aclose()
    if owned_engine is not None:
        await owned_engine.dispose()


app = FastAPI(title="billing-api", lifespan=lifespan)
app.include_router(public_router)
app.include_router(billing_router)
app.include_router(wallet_router)
app.include_router(models_router)
app.include_router(me_router)
app.include_router(admin_router)
app.include_router(org_admin_router)
app.include_router(org_internal_router)
app.include_router(webhook_router)
rest_errors.install(app)
# Added before the correlation-id middleware below, so it runs inside it and sees the id.
app.add_middleware(IdempotencyMiddleware)
# Added after it, so it runs first: an unauthenticated request never reaches the idempotency table.
app.add_middleware(GatewayAuthMiddleware)


@app.middleware("http")
async def correlation_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    cid = request.headers.get("X-Correlation-ID") or f"c-{uuid.uuid4().hex[:12]}"
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    correlation_id_var.set(cid)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    return {"status": "ok", "payment_mode": getattr(request.app.state, "payment_mode", "live")}


def _not_ready(reason: str) -> JSONResponse:
    return JSONResponse({"status": "not_ready", "reason": reason}, 503)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        return _not_ready("database not configured")
    if problem := await schema_problem(engine):
        return _not_ready(problem)

    client = getattr(request.app.state, "valkey", None)
    counters: ValkeyCounters | None = getattr(request.app.state, "counters", None)
    if client is None or counters is None:
        return _not_ready("valkey not configured")
    try:
        await aw(client.ping())
        if not await counters.ensure_scripts_loaded():
            return _not_ready("valkey scripts are not loaded")
        if unsafe := await config_problem(client):
            return _not_ready(unsafe)
    except Exception as exc:
        return _not_ready(f"valkey unavailable: {type(exc).__name__}")
    probe = getattr(request.app.state, "kafka_probe", None)
    if probe is not None and (problem := await probe()):
        return _not_ready(problem)
    return JSONResponse({"status": "ready"})


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)
