"""Redaction of personal data, webhook secret rotation, spans on the Razorpay adapter."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from billing.adapters.razorpay import gateway as gateway_module
from billing.adapters.razorpay import signatures
from billing.adapters.razorpay.gateway import RazorpayGateway
from billing.observability import configure_logging, init_sentry
from billing.redaction import REDACTED, redact, sentry_before_send
from billing.settings import Settings, webhook_secrets
from tests.support.clock import TestClock

PAYMENT = {
    "id": "pay_1",
    "amount": 49900,
    "email": "buyer@example.com",
    "contact": "+919999999999",
    "vpa": "buyer@okhdfc",
    "card": {"last4": "1111", "network": "Visa"},
    "card_id": "card_1",
    "notes": {"billing_ref": "r1", "note": "mail me at someone@else.org please"},
    "status": "captured",
}


def test_redact_blanks_sensitive_keys_at_any_depth_and_masks_emails_in_text() -> None:
    out = redact({"payload": {"payment": {"entity": PAYMENT}}, "list": [PAYMENT]})
    entity = out["payload"]["payment"]["entity"]
    for key in ("email", "contact", "vpa", "card", "card_id"):
        assert entity[key] == REDACTED
    assert entity["id"] == "pay_1" and entity["amount"] == 49900 and entity["status"] == "captured"
    assert entity["notes"]["note"] == f"mail me at {REDACTED} please"
    assert entity["notes"]["billing_ref"] == "r1"
    assert out["list"][0]["email"] == REDACTED
    assert PAYMENT["email"] == "buyer@example.com"  # the input is not modified
    assert redact(5) == 5 and redact(None) is None and redact(("a@b.io",)) == [REDACTED]


def test_logs_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.get_logger().info("webhook_seen", payload=PAYMENT, error="failed for a@b.io")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    text = json.dumps(json.loads(line))
    for secret in ("buyer@example.com", "+919999999999", "okhdfc", "a@b.io", "1111"):
        assert secret not in text
    assert json.loads(line)["payload"]["id"] == "pay_1"


def test_sentry_events_are_redacted_and_init_needs_a_dsn() -> None:
    event: dict[str, Any] = {
        "message": "bad payment for buyer@example.com",
        "extra": {"payment": PAYMENT},
        "exception": {"values": [{"value": "x"}]},
    }
    cleaned = sentry_before_send(event, None)
    assert cleaned is not None
    assert "buyer@example.com" not in json.dumps(cleaned) and "okhdfc" not in json.dumps(cleaned)
    assert init_sentry("", "test") is False


# --- webhook secret rotation -------------------------------------------------------------------


def test_either_secret_verifies_during_a_rotation() -> None:
    body = b'{"event":"subscription.charged"}'
    old = signatures.webhook_signature("old-secret", body)
    new = signatures.webhook_signature("new-secret", body)
    both = "new-secret,old-secret"
    assert signatures.verify_webhook(both, body, old) and signatures.verify_webhook(both, body, new)
    assert signatures.verify_webhook("new-secret", body, new)
    assert not signatures.verify_webhook("new-secret", body, old)  # after rotation ends
    assert not signatures.verify_webhook(both, body + b" ", new)
    assert not signatures.verify_webhook("", body, new)
    assert not signatures.verify_webhook(",,", body, new)


def settings(**kw: str) -> Settings:
    base = dict(
        billing_database_url="x", billing_migration_database_url="x", billing_valkey_url="x",
        kafka_bootstrap_servers="x", razorpay_key_id="k", razorpay_key_secret="s",
        razorpay_webhook_secret="primary", service_token_keys="t", supplier_state_code="10",
    )  # fmt: skip
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


def test_webhook_secrets_setting_adds_to_the_primary() -> None:
    assert webhook_secrets(settings()) == "primary"
    assert (
        webhook_secrets(settings(razorpay_webhook_secrets="next, older ,")) == "primary,next,older"
    )
    assert webhook_secrets(settings(razorpay_webhook_secrets="primary,next")) == "primary,next"
    gw = RazorpayGateway(
        "k",
        "s",
        webhook_secrets(settings(razorpay_webhook_secrets="next")),
        TestClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    body = b"{}"
    assert gw.verify_webhook_signature(body, signatures.webhook_signature("next", body))
    assert gw.verify_webhook_signature(body, signatures.webhook_signature("primary", body))


# --- spans -------------------------------------------------------------------------------------


@respx.mock
async def test_razorpay_calls_run_inside_a_named_span(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(gateway_module, "tracer", provider.get_tracer("test"))

    async def no_sleep(_: float) -> None:
        return None

    gw = RazorpayGateway("k", "s", "w", TestClock(datetime(2026, 1, 1, tzinfo=UTC)), sleep=no_sleep)
    respx.get("https://api.razorpay.com/v1/payments/pay_1").respond(
        200, json={"id": "pay_1", "amount": 100, "status": "captured", "created_at": 1793000000}
    )
    respx.post("https://api.razorpay.com/v1/subscriptions/sub_1/cancel").respond(
        200, json={"id": "sub_1", "plan_id": "p", "status": "cancelled"}
    )
    await gw.fetch_payment("pay_1")
    await gw.cancel_subscription("sub_1", at_cycle_end=False)
    assert [s.name for s in exporter.get_finished_spans()] == [
        "razorpay.get_payments",
        "razorpay.post_subscriptions",
    ]
    assert httpx.codes.OK == 200
