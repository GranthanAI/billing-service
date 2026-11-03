"""JSON event schemas: what billing publishes and what it accepts from other services."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from billing.adapters.kafka import schemas
from billing.adapters.kafka.codec import EventCodec
from billing.adapters.kafka.schemas import decode_consumed
from billing.app.events import SubscriptionChangedEvent
from billing.domain.catalog import OwnerType
from billing.domain.subscription import SubStatus
from tests.support.clock import TestClock

NOW = datetime(2026, 11, 1, 4, 45, tzinfo=UTC)


class Ids:
    def new(self) -> UUID:
        return UUID(int=7)


def test_produced_event_is_flat_json_with_the_envelope_first() -> None:
    event = SubscriptionChangedEvent(
        UUID(int=1), OwnerType.USER, UUID(int=2), UUID(int=3), "pro", SubStatus.ACTIVE,
        None, SubStatus.INCOMPLETE, NOW,
    )  # fmt: skip
    body = json.loads(EventCodec(Ids(), TestClock(NOW)).encode(event).payload)
    assert list(body)[:6] == [
        "event_type", "schema_version", "event_id", "occurred_at", "correlation_id", "producer",
    ]  # fmt: skip
    assert body["event_type"] == "subscription.changed"
    assert body["schema_version"] == "1.0" and body["producer"] == "billing-service"
    assert body["event_id"] == str(UUID(int=7))
    assert body["occurred_at"] == "2026-11-01T04:45:00Z"
    assert body["plan_code"] == "pro" and body["previous_plan_code"] is None


def test_produced_events_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        schemas.PaymentFailedMsg.model_validate(
            {
                "event_type": "payment.failed", "event_id": "e", "occurred_at": NOW.isoformat(),
                "billing_account_id": "a", "subscription_id": "s", "gateway_payment_id": "p",
                "amount_paise": 1, "reason": "x", "surprise": True,
            }
        )  # fmt: skip


CHAT = {
    "conversation_id": "c1", "user_id": "u1", "request_message_id": "m1",
    "response_message_id": "r1", "status": "success", "timestamp": "2026-11-01T04:45:00Z",
    "hold_id": "h1", "model_group": "smart", "model": "claude-haiku-5-5",
    "calls": [
        {"provider": "anthropic", "model": "claude-haiku-5-5", "purpose": "answer",
         "input_tokens": 6000, "cached_input_tokens": 0, "output_tokens": 700,
         "usage_estimated": False},
    ],
    "schema_version": "2.2", "event_type": "chat.response.generated", "a_new_field": [1, 2],
}  # fmt: skip


def test_consumed_chat_event_ignores_extra_fields_and_reads_the_calls() -> None:
    ev = decode_consumed("chat.response.generated", json.dumps(CHAT).encode())
    assert isinstance(ev, schemas.ChatResponseGenerated)
    assert ev.status == "success" and ev.hold_id == "h1"
    assert ev.calls[0].input_tokens == 6000 and ev.calls[0].usage_estimated is False
    assert ev.occurred_at == NOW
    assert ev.dedupe_key == "chat.response.generated:m1"


def test_consumed_chat_event_with_no_hold_and_a_cancelled_twin() -> None:
    body = {**CHAT, "hold_id": None}
    ev = decode_consumed("chat.response.generated", json.dumps(body).encode())
    assert isinstance(ev, schemas.ChatResponseGenerated) and ev.hold_id is None
    cancelled = decode_consumed(
        "chat.response.cancelled", json.dumps({**CHAT, "status": "cancelled"}).encode()
    )
    assert isinstance(cancelled, schemas.ChatResponseCancelled)
    assert cancelled.dedupe_key == "chat.response.cancelled:m1"


def test_consumed_manim_youtube_and_user_deleted() -> None:
    manim = decode_consumed(
        "artifact.manim.rendered",
        json.dumps(
            {"event_id": "e1", "occurred_at": "2026-11-01T04:45:00Z", "user_id": "u", "job_id": "j",
             "hold_id": "h", "status": "error", "calls": [], "schema_version": "1.0"}
        ).encode(),
    )  # fmt: skip
    assert isinstance(manim, schemas.ArtifactManimRendered)
    assert (manim.job_id, manim.status, manim.dedupe_key) == ("j", "error", "e1")
    video = decode_consumed(
        "file.youtube.ingested",
        json.dumps(
            {"event_id": "e2", "occurred_at": "2026-11-01T04:45:00Z", "user_id": "u",
             "source_id": "yt1", "hold_id": None, "status": "success"}
        ).encode(),
    )  # fmt: skip
    assert isinstance(video, schemas.FileYoutubeIngested) and video.source_id == "yt1"
    gone = decode_consumed(
        "user.deleted",
        json.dumps(
            {"event_id": "e3", "occurred_at": "2026-11-01T04:45:00Z", "user_id": "u"}
        ).encode(),
    )
    assert isinstance(gone, schemas.UserDeleted) and gone.dedupe_key == "e3"


@pytest.mark.parametrize(
    "body", [b"not json", b"{}", json.dumps({**CHAT, "status": "weird"}).encode()]
)
def test_malformed_consumed_events_are_rejected(body: bytes) -> None:
    with pytest.raises(ValidationError):
        decode_consumed("chat.response.generated", body)


def test_unknown_topic() -> None:
    with pytest.raises(KeyError):
        decode_consumed("nope", b"{}")
