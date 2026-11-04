"""Usage: the pure parts (status rule, call normalisation, cost helpers, event conversion)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from billing.adapters.kafka.usage_consumer import to_usage_event
from billing.app import topics
from billing.app.usage import UsageCall, UsageEvent, _domain_calls, effective_status
from billing.domain.catalog import ReserveKind
from billing.domain.rating import LlmCall as DomainCall
from billing.domain.rating import (
    ModelPrice,
    PriceBook,
    cost_micro_inr,
    inr_to_micro_inr,
    usd_to_micro_inr,
)

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


def event(status: str, *outputs: int) -> UsageEvent:
    calls = tuple(UsageCall("p", "m", "answer", 10, 0, n) for n in outputs)
    return UsageEvent(
        ReserveKind.MESSAGE,
        "m1",
        "k",
        uuid4(),
        NOW,
        status,
        calls=calls,  # type: ignore[arg-type]
    )


def test_cancelled_is_a_success_only_when_someone_saw_output() -> None:
    assert effective_status(event("success", 5)) == "success"
    assert effective_status(event("error", 5)) == "error"
    assert effective_status(event("cancelled", 0, 0)) == "error"
    assert effective_status(event("cancelled", 0, 3)) == "success"
    assert effective_status(event("cancelled")) == "error"  # no calls at all


def test_the_last_answer_is_primary_and_everything_else_is_cost_only() -> None:
    first = UsageCall("a", "m", "answer", 100, 0, 1)
    last = UsageCall("a", "m", "answer", 200, 0, 2)
    helper = UsageCall("b", "m2", "memory", 10, 0, 1)
    odd = UsageCall("b", "m2", "brand_new_purpose", 10, 0, 1)
    domain, primary = _domain_calls((first, helper, last, odd))
    assert primary is last
    assert [c.purpose for c in domain] == [
        "request_analysis",
        "memory",
        "answer",
        "request_analysis",
    ]
    assert _domain_calls(())[1] is None
    assert _domain_calls((helper,))[1] is None  # no answer call


def test_cached_tokens_are_clamped_and_negatives_floored() -> None:
    domain, _ = _domain_calls((UsageCall("a", "m", "answer", 100, 500, -3),))
    assert (domain[0].input_tokens, domain[0].cached_input_tokens, domain[0].output_tokens) == (
        100, 100, 0,
    )  # fmt: skip


def test_cost_helpers_round_half_up_once() -> None:
    book = PriceBook({"m": ModelPrice(Decimal("0.10"), Decimal("0.01"), Decimal("0.50"))})
    calls = [DomainCall("p", "m", "answer", 6000, 2000, 700)]
    # (4000*0.10 + 2000*0.01 + 700*0.50) / 1e6 USD = 0.00077 USD; at 95 = 0.07315 INR
    assert cost_micro_inr(calls, book, Decimal("95")) == 73_150
    assert usd_to_micro_inr(Decimal("0.01"), Decimal("95")) == 950_000
    assert inr_to_micro_inr(Decimal("0.30")) == 300_000
    assert inr_to_micro_inr(Decimal("0.0000005")) == 1  # half up
    assert cost_micro_inr([], book, Decimal("95")) == 0


def chat_body(**over: object) -> bytes:
    body = {
        "conversation_id": "c", "user_id": str(uuid4()), "request_message_id": "rm1",
        "status": "success", "timestamp": "2026-11-10T06:00:00Z", "hold_id": "h1",
        "model_group": "smart", "model": "claude-haiku-5-5",
        "calls": [{"provider": "anthropic", "model": "claude-haiku-5-5", "purpose": "answer",
                   "input_tokens": 10, "output_tokens": 2}],
    }  # fmt: skip
    body.update(over)
    return json.dumps(body).encode()


def test_chat_events_become_usage_events() -> None:
    ev = to_usage_event(topics.CHAT_RESPONSE_GENERATED, chat_body())
    assert ev.kind is ReserveKind.MESSAGE and ev.source_id == "rm1"
    assert ev.dedupe_key == "chat.response.generated:rm1"
    assert (ev.hold_id, ev.model_group, ev.status) == ("h1", "smart", "success")
    assert ev.occurred_at == NOW and ev.calls[0].cached_input_tokens == 0
    cancelled = to_usage_event(
        topics.CHAT_RESPONSE_CANCELLED, chat_body(status="cancelled", hold_id=None)
    )
    assert cancelled.status == "cancelled" and cancelled.hold_id is None
    assert cancelled.dedupe_key == "chat.response.cancelled:rm1"


def test_manim_and_youtube_events_become_usage_events() -> None:
    uid = str(uuid4())
    manim = to_usage_event(
        topics.ARTIFACT_MANIM_RENDERED,
        json.dumps({"event_id": "e1", "occurred_at": "2026-11-10T06:00:00Z", "user_id": uid,
                    "job_id": "j9", "hold_id": "", "status": "error"}).encode(),
    )  # fmt: skip
    assert manim.kind is ReserveKind.MANIM_VIDEO and manim.source_id == "j9"
    assert manim.hold_id is None and manim.dedupe_key == "e1"
    video = to_usage_event(
        topics.FILE_YOUTUBE_INGESTED,
        json.dumps({"event_id": "e2", "occurred_at": "2026-11-10T06:00:00Z", "user_id": uid,
                    "source_id": "yt7", "hold_id": "h", "status": "success"}).encode(),
    )  # fmt: skip
    assert video.kind is ReserveKind.YOUTUBE_VIDEO and video.source_id == "yt7"


def test_malformed_and_unroutable_input_raises() -> None:
    with pytest.raises(ValidationError):
        to_usage_event(topics.CHAT_RESPONSE_GENERATED, b"{nope")
    with pytest.raises(ValidationError):
        to_usage_event(topics.CHAT_RESPONSE_GENERATED, chat_body(user_id=None))
    with pytest.raises(ValidationError):
        to_usage_event(topics.USER_DELETED, b"{}")  # a known topic, but an empty body
    with pytest.raises(KeyError):
        to_usage_event("no.such.topic", b"{}")
    with pytest.raises(ValueError):
        to_usage_event(topics.USER_DELETED, json.dumps(
            {"event_id": "e", "occurred_at": "2026-11-10T06:00:00Z", "user_id": str(uuid4())}
        ).encode())  # fmt: skip
