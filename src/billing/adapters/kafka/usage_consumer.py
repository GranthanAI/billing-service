"""The billing-usage consumer: Kafka bytes -> UsageEvent -> UsageService."""

from uuid import UUID

from billing.adapters.kafka import schemas
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.producer import KafkaProducer
from billing.app import topics
from billing.app.usage import UsageCall, UsageEvent, UsageService
from billing.domain.catalog import ReserveKind
from billing.observability import USAGE_EVENTS

GROUP_ID = "billing-usage"
USAGE_TOPICS = [
    topics.CHAT_RESPONSE_GENERATED,
    topics.CHAT_RESPONSE_CANCELLED,
    topics.ARTIFACT_MANIM_RENDERED,
    topics.FILE_YOUTUBE_INGESTED,
]


def _calls(calls: list[schemas.LlmCall]) -> tuple[UsageCall, ...]:
    return tuple(
        UsageCall(
            c.provider, c.model, c.purpose, c.input_tokens, c.cached_input_tokens, c.output_tokens
        )
        for c in calls
    )


def to_usage_event(topic: str, payload: bytes) -> UsageEvent:
    """Raises on a malformed body or an unknown topic; the consumer dead-letters it."""
    event = schemas.decode_consumed(topic, payload)
    match event:
        case schemas.ChatResponseGenerated() | schemas.ChatResponseCancelled():
            return UsageEvent(
                kind=ReserveKind.MESSAGE,
                source_id=event.request_message_id,
                dedupe_key=event.dedupe_key,
                user_id=UUID(event.user_id),
                occurred_at=event.timestamp,
                status=event.status,
                hold_id=event.hold_id or None,
                model_group=event.model_group,
                model=event.model,
                calls=_calls(event.calls),
            )
        case schemas.ArtifactManimRendered():
            return UsageEvent(
                kind=ReserveKind.MANIM_VIDEO,
                source_id=event.job_id,
                dedupe_key=event.dedupe_key,
                user_id=UUID(event.user_id),
                occurred_at=event.occurred_at,
                status=event.status,
                hold_id=event.hold_id or None,
                calls=_calls(event.calls),
            )
        case schemas.FileYoutubeIngested():
            return UsageEvent(
                kind=ReserveKind.YOUTUBE_VIDEO,
                source_id=event.source_id,
                dedupe_key=event.dedupe_key,
                user_id=UUID(event.user_id),
                occurred_at=event.occurred_at,
                status=event.status,
                hold_id=event.hold_id or None,
            )
    raise ValueError(f"not a usage event: {topic}")


def make_usage_consumer(
    service: UsageService,
    *,
    bootstrap_servers: str,
    client_id: str,
    producer: KafkaProducer,
    group_id: str = GROUP_ID,
    backoff: tuple[float, ...] | None = None,
) -> KafkaEventConsumer[UsageEvent]:
    async def handle(event: UsageEvent) -> None:
        await service.apply(event)

    return KafkaEventConsumer(
        bootstrap_servers=bootstrap_servers,
        client_id=client_id,
        topics=USAGE_TOPICS,
        group_id=group_id,
        decoder=to_usage_event,
        handler=handle,
        dlq_topic=topics.BILLING_USAGE_DLQ,
        producer=producer,
        **({"backoff": backoff} if backoff is not None else {}),  # type: ignore[arg-type]
        on_dead_letter=lambda topic: USAGE_EVENTS.labels(kind=topic, result="dlq").inc(),
    )
