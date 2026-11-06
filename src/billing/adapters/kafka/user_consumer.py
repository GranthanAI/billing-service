"""The billing-user consumer: `user.deleted` -> UserDeletionService."""

from uuid import UUID

from billing.adapters.kafka import schemas
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.producer import KafkaProducer
from billing.app import topics
from billing.app.user_deletion import UserDeletionService

GROUP_ID = "billing-user"


def decode_user_deleted(topic: str, payload: bytes) -> schemas.UserDeleted:
    event = schemas.decode_consumed(topic, payload)
    if not isinstance(event, schemas.UserDeleted):
        raise ValueError(f"not a user.deleted event: {topic}")
    UUID(event.user_id)  # a malformed id is dead-lettered at once, not retried
    return event


def make_user_consumer(
    service: UserDeletionService,
    *,
    bootstrap_servers: str,
    client_id: str,
    producer: KafkaProducer,
    group_id: str = GROUP_ID,
    backoff: tuple[float, ...] | None = None,
) -> KafkaEventConsumer[schemas.UserDeleted]:
    async def handle(event: schemas.UserDeleted) -> None:
        await service.apply(UUID(event.user_id), event.dedupe_key)

    return KafkaEventConsumer(
        bootstrap_servers=bootstrap_servers,
        client_id=client_id,
        topics=[topics.USER_DELETED],
        group_id=group_id,
        decoder=decode_user_deleted,
        handler=handle,
        dlq_topic=topics.BILLING_USER_DLQ,
        producer=producer,
        **({"backoff": backoff} if backoff is not None else {}),  # type: ignore[arg-type]
    )
