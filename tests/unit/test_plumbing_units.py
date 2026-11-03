import asyncio
import filecmp
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from billing.adapters.ids import SequentialIds
from billing.adapters.kafka.codec import EventCodec, decode_event
from billing.app import topics
from billing.app.context import correlation_id_var
from billing.app.events import (
    InvoiceIssuedEvent,
    PaymentFailedEvent,
    SubscriptionChangedEvent,
    UsageThresholdReachedEvent,
)
from billing.domain.catalog import OwnerType
from billing.domain.money import Paise
from billing.domain.subscription import SubStatus
from billing.jobs.scheduler import Recurring, dedupe_key, slot_start
from billing.main_worker import install_signal_handlers
from tests.support.clock import TestClock

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def u(n: int) -> UUID:
    return UUID(int=n)


EVENTS = [
    SubscriptionChangedEvent(
        u(1), OwnerType.USER, u(2), u(3), "max", SubStatus.ACTIVE, "pro", SubStatus.ACTIVE, NOW
    ),
    SubscriptionChangedEvent(
        u(1), OwnerType.ORG, u(2), u(3), "free", SubStatus.ENDED, None, None, None
    ),
    PaymentFailedEvent(u(1), u(3), "pay_123", Paise(49900), "card_declined", NOW),
    PaymentFailedEvent(u(1), u(3), "pay_124", Paise(1), "x", None),
    UsageThresholdReachedEvent(u(9), u(1), "messages_per_day", 80, 48, 60, NOW),
    InvoiceIssuedEvent(u(1), u(4), "GA/2627/000001", "tax_invoice", Paise(49900), "inv/1.pdf"),
    InvoiceIssuedEvent(u(1), u(4), "CN/2627/000001", "credit_note", Paise(100), None),
]


@pytest.mark.parametrize("event", EVENTS)
def test_codec_round_trip_and_envelope(event: object) -> None:
    token = correlation_id_var.set("c-abc123")
    try:
        codec = EventCodec(SequentialIds(100), TestClock(NOW))
        encoded = codec.encode(event)  # type: ignore[arg-type]
    finally:
        correlation_id_var.reset(token)
    envelope, decoded = decode_event(encoded.event_type, encoded.payload)
    assert decoded == event
    assert envelope.event_id == str(u(100))
    assert envelope.event_type == encoded.topic
    assert envelope.producer == "billing-service" and envelope.schema_version == "1.0"
    assert envelope.correlation_id == "c-abc123"
    assert envelope.occurred_at == NOW
    assert encoded.headers["event_id"] == envelope.event_id
    assert encoded.headers["event_type"] == encoded.topic
    assert encoded.headers["correlation_id"] == "c-abc123"


def test_keys_and_topics() -> None:
    codec = EventCodec(SequentialIds(), TestClock(NOW))
    by_type = {type(e): codec.encode(e) for e in EVENTS}
    assert by_type[SubscriptionChangedEvent].topic == topics.SUBSCRIPTION_CHANGED
    assert by_type[SubscriptionChangedEvent].key == str(u(1))
    assert by_type[PaymentFailedEvent].topic == topics.PAYMENT_FAILED
    assert by_type[UsageThresholdReachedEvent].topic == topics.USAGE_THRESHOLD_REACHED
    assert by_type[UsageThresholdReachedEvent].key == str(u(9))  # the user, not the account
    assert by_type[InvoiceIssuedEvent].topic == topics.INVOICE_ISSUED
    assert "correlation_id" not in by_type[InvoiceIssuedEvent].headers  # none set


def test_unknown_event_type() -> None:
    with pytest.raises(ValueError):
        decode_event("nope", b"")


def test_topic_names_are_exactly_the_agreed_ones() -> None:
    assert set(topics.ALL) == {
        "chat.response.generated",
        "artifact.manim.rendered",
        "file.youtube.ingested",
        "user.deleted",
        "subscription.changed",
        "payment.failed",
        "usage.threshold_reached",
        "invoice.issued",
        "billing.usage.dlq",
        "billing.user.dlq",
    }


def test_compose_init_creates_every_topic() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    listed = set(re.findall(r"--topic ([\w.]+)", compose))
    assert listed == set(topics.ALL)
    assert "--partitions 6" in compose and "--replication-factor 1" in compose


# --- scheduler slots ----------------------------------------------------------------------------


def test_daily_slot_follows_the_ist_calendar() -> None:
    job = Recurring("partitions.ensure", daily_at="04:00 IST")
    before = datetime(2026, 11, 1, 22, 29, 59, tzinfo=UTC)  # 03:59:59 IST on 2 Nov
    after = datetime(2026, 11, 1, 22, 30, 0, tzinfo=UTC)  # 04:00:00 IST on 2 Nov
    assert slot_start(job, before) == datetime(2026, 10, 31, 22, 30, tzinfo=UTC)
    assert slot_start(job, after) == datetime(2026, 11, 1, 22, 30, tzinfo=UTC)
    assert dedupe_key(job, before) != dedupe_key(job, after)
    assert dedupe_key(job, after) == dedupe_key(job, after + timedelta(hours=23))
    assert dedupe_key(job, after).startswith("partitions.ensure:2026-11-01T22:30:00")


def test_every_and_monthly_slots() -> None:
    every = Recurring("holds.sweep", every=timedelta(seconds=30))
    assert slot_start(every, datetime(2026, 11, 1, 10, 0, 29, tzinfo=UTC)) == NOW
    assert slot_start(every, datetime(2026, 11, 1, 10, 0, 30, tzinfo=UTC)) == NOW + timedelta(
        seconds=30
    )
    monthly = Recurring("revenue.recognise", monthly_on=1, at="02:00 IST")
    # 1 Dec 02:00 IST = 30 Nov 20:30 UTC
    assert slot_start(monthly, datetime(2026, 11, 30, 20, 30, tzinfo=UTC)) == datetime(
        2026, 11, 30, 20, 30, tzinfo=UTC
    )
    assert slot_start(monthly, datetime(2026, 11, 30, 20, 29, tzinfo=UTC)) == datetime(
        2026, 10, 31, 20, 30, tzinfo=UTC
    )
    assert slot_start(monthly, datetime(2027, 1, 15, tzinfo=UTC)) == datetime(
        2026, 12, 31, 20, 30, tzinfo=UTC
    )
    assert slot_start(monthly, datetime(2027, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 12, 31, 20, 30, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        slot_start(every, datetime(2026, 1, 1))


def test_recurring_validation() -> None:
    with pytest.raises(ValueError):
        Recurring("x")
    with pytest.raises(ValueError):
        Recurring("x", every=timedelta(seconds=1), daily_at="01:00 IST")
    with pytest.raises(ValueError):
        Recurring("x", monthly_on=31, at="01:00 IST")
    with pytest.raises(ValueError):
        slot_start(Recurring("x", daily_at="01:00 UTC"), NOW)


# --- worker signal handling ---------------------------------------------------------------------


async def test_sigterm_sets_the_stop_event() -> None:
    stop = asyncio.Event()
    install_signal_handlers(stop)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(stop.wait(), 2)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


# --- generated code is current ------------------------------------------------------------------


def test_generated_code_matches_the_protos(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_proto.py"), str(out)],
        check=True,
        capture_output=True,
    )
    committed = ROOT / "src" / "billing" / "gen"
    for fresh in out.rglob("*"):
        if fresh.is_file() and "__pycache__" not in fresh.parts:
            existing = committed / fresh.relative_to(out)
            assert existing.exists(), f"{existing} is not committed; run make proto"
            assert filecmp.cmp(fresh, existing, shallow=False), f"{existing} is stale: make proto"


def test_ci_runs_buf_and_the_generated_code_check() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "buf lint" in ci and "buf breaking" in ci
    assert "make proto" in ci and "git diff --exit-code src/billing/gen" in ci
