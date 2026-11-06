"""Gauges, counters, spans, the database grants and the readiness probe."""

import httpx
import pytest
import sqlalchemy as sa
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

import billing.app.usage as usage_module
import billing.jobs.webhooks as webhooks_module
from billing.adapters.kafka.health import KafkaProbe
from billing.app.gauges import refresh_gauges
from billing.main_api import app
from billing.observability import PAYMENTS
from tests.integration.billing_env import PRO_MONTH, Env

SPEC = REGISTRY.get_sample_value


def metric(name: str, **labels: str) -> float:
    return SPEC(name, labels) or 0.0


# --- gauges and counters ---------------------------------------------------------------------------  # noqa: E501


async def test_gauges_show_the_inbox_and_subscriptions_and_reset_stale_pairs(env: Env) -> None:
    options = await env.activate()
    await env.deliver(
        env.gateway.authenticate(options.gateway_subscription_id)
    )  # one still waiting
    await refresh_gauges(env.bctx.uow_factory)
    assert metric("billing_webhook_inbox_backlog", status="processed") == 1  # the charge
    assert metric("billing_webhook_inbox_backlog", status="ignored") == 1  # authenticated
    assert metric("billing_webhook_inbox_backlog", status="received") == 1
    assert metric("billing_webhook_inbox_backlog", status="failed") == 0
    assert metric("billing_subscriptions", plan="pro", status="active") == 1

    await env.service.cancel(env.user)
    await refresh_gauges(env.bctx.uow_factory)
    assert metric("billing_subscriptions", plan="pro", status="cancelling") == 1
    assert SPEC("billing_subscriptions", {"plan": "pro", "status": "active"}) is None  # not stale


async def test_a_captured_charge_and_a_failed_payment_are_counted(env: Env) -> None:
    before = metric("billing_payments_total", method="card", status="captured")
    failed_before = metric("billing_payments_total", method="card", status="failed")
    options = await env.activate()
    assert metric("billing_payments_total", method="card", status="captured") == before + 1
    await env.deliver(env.gateway.charge(options.gateway_subscription_id, PRO_MONTH))  # a renewal
    await env.process()
    assert metric("billing_payments_total", method="card", status="captured") == before + 2

    hook = env.gateway._webhook(
        "payment_failed", payment={"id": "pay_bad", "method": "card", "status": "failed"}
    )
    await env.deliver(hook)
    await env.process()
    assert metric("billing_payments_total", method="card", status="failed") == failed_before + 1
    assert PAYMENTS is not None


async def test_webhook_processing_and_usage_run_inside_named_spans(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(webhooks_module, "tracer", provider.get_tracer("test"))
    monkeypatch.setattr(usage_module, "tracer", provider.get_tracer("test"))
    await env.activate()
    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("billing.webhook.process") == 2
    span = next(s for s in exporter.get_finished_spans() if s.name == "billing.webhook.process")
    assert span.attributes is not None and str(span.attributes["webhook.event_type"]).startswith(
        "subscription."
    )


# --- grants ---------------------------------------------------------------------------------------------  # noqa: E501

READ = {"SELECT"}
APPEND = {"SELECT", "INSERT"}
UPDATE = {"SELECT", "INSERT", "UPDATE"}
FULL = {"SELECT", "INSERT", "UPDATE", "DELETE"}
EXPECTED: dict[str, set[str]] = {
    # append-only: the ledger, usage and audit can never be changed or removed by the app
    "ledger_entries": APPEND, "ledger_transactions": APPEND, "usage_events": APPEND,
    "subscription_history": APPEND, "admin_audit": APPEND, "reconciliation_runs": APPEND,
    # written only by the migrator (catalog sync)
    "plan_versions": READ, "models": READ, "alembic_version": READ,
    "model_prices": APPEND, "fx_rates": APPEND,
    # state that legitimately changes
    "billing_accounts": UPDATE, "subscriptions": UPDATE, "subscription_changes": UPDATE,
    "plan_overrides": UPDATE, "payments": UPDATE, "refunds": UPDATE, "invoices": UPDATE,
    "invoice_lines": UPDATE, "invoice_counters": UPDATE, "ledger_accounts": UPDATE,
    "ledger_balances": UPDATE, "credit_lots": UPDATE, "usage_daily": UPDATE,
    "provider_costs": UPDATE,
    "webhook_inbox": FULL,  # housekeeping prunes old rows
    # plumbing the app owns
    "processed_events": FULL, "outbox": FULL, "idempotency_keys": FULL, "jobs": FULL,
}  # fmt: skip


async def test_the_app_role_has_exactly_the_grants_the_design_allows(
    app_engine: AsyncEngine,
) -> None:
    async with app_engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'billing_app' AND table_schema = 'billing'"
            )
        )
        granted: dict[str, set[str]] = {}
        for table, privilege in rows:
            name = "usage_events" if table.startswith("usage_events_") else table  # partitions
            granted.setdefault(name, set()).add(privilege)
    # TRUNCATE, TRIGGER and REFERENCES are never granted
    assert {p for ps in granted.values() for p in ps} <= {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert granted == EXPECTED, {
        t: (granted.get(t), EXPECTED.get(t)) for t in set(granted) | set(EXPECTED)
        if granted.get(t) != EXPECTED.get(t)
    }  # fmt: skip


async def test_the_append_only_tables_really_refuse_updates_and_deletes(env: Env) -> None:
    await env.activate()
    from sqlalchemy.exc import DBAPIError

    for statement in (
        "UPDATE billing.ledger_entries SET amount_paise = 1",
        "DELETE FROM billing.ledger_transactions",
        "UPDATE billing.subscription_history SET trigger = 'x'",
        "DELETE FROM billing.admin_audit",
    ):
        async with env.app_engine.connect() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(sa.text(statement))


# --- readiness ------------------------------------------------------------------------------------------------  # noqa: E501


async def test_the_kafka_probe_sees_a_broker_and_a_missing_one(kafka: str) -> None:
    assert await KafkaProbe(kafka)() is None
    problem = await KafkaProbe("localhost:1", timeout_ms=500)()
    assert problem is not None and problem.startswith("kafka unavailable")


async def test_the_kafka_probe_caches_its_answer() -> None:
    now = [0.0]
    probe = KafkaProbe(
        "localhost:1", ok_ttl=15, fail_ttl=5, timeout_ms=300, monotonic=lambda: now[0]
    )
    first = await probe()
    now[0] = 4.0  # inside the failure window: no new connection attempt
    assert await probe() == first
    now[0] = 6.0  # outside it: asks again
    assert await probe() == first


async def ready(env: Env, kafka_probe: object, vk: object) -> httpx.Response:
    app.state.engine = env.app_engine
    app.state.valkey = vk
    app.state.counters = env.counters
    app.state.kafka_probe = kafka_probe
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.get("/readyz")
    finally:
        app.state.kafka_probe = None


async def test_readyz_includes_kafka(env: Env, kafka: str) -> None:
    import valkey.asyncio as valkey_asyncio

    from billing.adapters.valkey.client import make_valkey

    url = env.counters._c.connection_pool.connection_kwargs  # the session Valkey
    client: valkey_asyncio.Valkey = make_valkey(f"redis://{url['host']}:{url['port']}/0")
    try:
        ok = await ready(env, KafkaProbe(kafka), client)
        assert ok.status_code == 200 and ok.json() == {"status": "ready"}
        down = await ready(env, KafkaProbe("localhost:1", timeout_ms=500), client)
        assert down.status_code == 503 and "kafka" in down.json()["reason"]
    finally:
        await client.aclose()
