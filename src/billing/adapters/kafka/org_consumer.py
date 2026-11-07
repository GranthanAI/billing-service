"""The billing-org consumer: org.member.joined / org.member.left -> OrgMembershipService."""

from uuid import UUID

from billing.adapters.kafka import schemas
from billing.adapters.kafka.consumer import KafkaEventConsumer
from billing.adapters.kafka.producer import KafkaProducer
from billing.app import topics
from billing.app.org_membership import OrgMembershipService

GROUP_ID = "billing-org"
ORG_TOPICS = [topics.ORG_MEMBER_JOINED, topics.ORG_MEMBER_LEFT]


def decode_member_event(topic: str, payload: bytes) -> schemas.OrgMemberEvent:
    event = schemas.decode_consumed(topic, payload)
    if not isinstance(event, schemas.OrgMemberEvent):
        raise ValueError(f"not an org membership event: {topic}")
    UUID(event.user_id)  # a malformed id is dead-lettered at once, not retried
    UUID(event.org_id)
    return event


def make_org_consumer(
    service: OrgMembershipService,
    *,
    bootstrap_servers: str,
    client_id: str,
    producer: KafkaProducer,
    group_id: str = GROUP_ID,
    backoff: tuple[float, ...] | None = None,
) -> KafkaEventConsumer[schemas.OrgMemberEvent]:
    async def handle(event: schemas.OrgMemberEvent) -> None:
        await service.apply(
            joined=isinstance(event, schemas.OrgMemberJoined),
            user_id=UUID(event.user_id),
            org_id=UUID(event.org_id),
            dedupe_key=event.dedupe_key,
        )

    return KafkaEventConsumer(
        bootstrap_servers=bootstrap_servers,
        client_id=client_id,
        topics=ORG_TOPICS,
        group_id=group_id,
        decoder=decode_member_event,
        handler=handle,
        dlq_topic=topics.BILLING_ORG_DLQ,
        producer=producer,
        **({"backoff": backoff} if backoff is not None else {}),  # type: ignore[arg-type]
    )
