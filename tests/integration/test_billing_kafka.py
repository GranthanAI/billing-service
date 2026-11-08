"""subscription.changed travels outbox -> Kafka -> a consumer."""

from billing.adapters.kafka.codec import decode_event
from billing.adapters.kafka.outbox_relay import OutboxRelay
from billing.adapters.kafka.producer import KafkaProducer
from billing.app import topics
from billing.app.events import SubscriptionChangedEvent
from billing.domain.subscription import SubStatus
from tests.integration.billing_env import Env
from tests.integration.kafka_support import make_topic, read_all


async def test_subscription_changed_is_consumed_from_kafka(env: Env, kafka: str) -> None:
    await make_topic(kafka, topics.SUBSCRIPTION_CHANGED)
    await make_topic(kafka, topics.ENTITLEMENTS_CHANGED)
    await env.activate()
    account = await env.scalar("SELECT id FROM billing.billing_accounts")

    producer = KafkaProducer(kafka, "test")
    await producer.start()
    try:
        relay = OutboxRelay(env.app_engine, env.bctx.ids, env.clock, producer)
        # the transition queued subscription.changed and entitlements.changed together
        assert await relay.relay_once() == 2
    finally:
        await producer.stop()

    mine = []
    for rec in await read_all(kafka, topics.SUBSCRIPTION_CHANGED):
        if rec.key == str(account).encode():
            _, event = decode_event(topics.SUBSCRIPTION_CHANGED, rec.value)
            assert isinstance(event, SubscriptionChangedEvent)
            mine.append(event)
    assert len(mine) == 1
    event = mine[0]
    assert (event.plan_code, event.status) == ("pro", SubStatus.ACTIVE)
    assert event.previous_status is SubStatus.INCOMPLETE
    assert event.owner_id == env.user and event.owner_type.value == "user"
    assert event.current_period_end is not None

    told = [
        decode_event(topics.ENTITLEMENTS_CHANGED, rec.value)[1]
        for rec in await read_all(kafka, topics.ENTITLEMENTS_CHANGED)
        if rec.key == str(env.user).encode()
    ]
    assert [(e.reason, e.user_id) for e in told] == [("subscription", env.user)]  # type: ignore[attr-defined]
