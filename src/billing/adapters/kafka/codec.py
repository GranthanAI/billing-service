"""Domain events <-> protobuf. The only module that knows both."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from opentelemetry import trace

from billing.app import topics
from billing.app.context import correlation_id_var
from billing.app.events import (
    InvoiceIssuedEvent,
    OutboxEvent,
    PaymentFailedEvent,
    SubscriptionChangedEvent,
    UsageThresholdReachedEvent,
)
from billing.app.ports import Clock, IdGenerator
from billing.domain.catalog import OwnerType
from billing.domain.money import Paise
from billing.domain.subscription import SubStatus
from billing.gen.granthan.events.v1 import billing_events_pb2 as pb
from billing.gen.granthan.events.v1 import envelope_pb2

PRODUCER = "billing-service"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class EncodedEvent:
    topic: str
    key: str
    event_type: str
    payload: bytes
    headers: dict[str, str]


def _ts(dt: datetime) -> Timestamp:
    t = Timestamp()
    t.FromDatetime(dt.astimezone(UTC))
    return t


def _dt(msg: Message, field: str) -> datetime | None:
    if not msg.HasField(field):
        return None
    ts: Timestamp = getattr(msg, field)
    return ts.ToDatetime(tzinfo=UTC)


def traceparent() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


class EventCodec:
    def __init__(self, ids: IdGenerator, clock: Clock) -> None:
        self._ids = ids
        self._clock = clock

    def _envelope(self, event_type: str) -> tuple[envelope_pb2.Envelope, dict[str, str]]:
        event_id = str(self._ids.new())
        correlation = correlation_id_var.get()
        env = envelope_pb2.Envelope(
            event_id=event_id,
            event_type=event_type,
            occurred_at=_ts(self._clock.now()),
            correlation_id=correlation,
            producer=PRODUCER,
            schema_version=SCHEMA_VERSION,
        )
        headers = {"event_id": event_id, "event_type": event_type}
        if correlation:
            headers["correlation_id"] = correlation
        tp = traceparent()
        if tp:
            headers["traceparent"] = tp
        return env, headers

    def encode(self, event: OutboxEvent) -> EncodedEvent:
        match event:
            case SubscriptionChangedEvent():
                topic, key = topics.SUBSCRIPTION_CHANGED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg: Message = pb.SubscriptionChanged(
                    envelope=env,
                    billing_account_id=str(event.billing_account_id),
                    owner_type=event.owner_type.value,
                    owner_id=str(event.owner_id),
                    subscription_id=str(event.subscription_id),
                    plan_code=event.plan_code,
                    status=event.status.value,
                    previous_plan_code=event.previous_plan_code or "",
                    previous_status=event.previous_status.value if event.previous_status else "",
                    current_period_end=_ts(event.current_period_end)
                    if event.current_period_end
                    else None,
                )
            case PaymentFailedEvent():
                topic, key = topics.PAYMENT_FAILED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg = pb.PaymentFailed(
                    envelope=env,
                    billing_account_id=str(event.billing_account_id),
                    subscription_id=str(event.subscription_id),
                    gateway_payment_id=event.gateway_payment_id,
                    amount_paise=event.amount,
                    reason=event.reason,
                    next_retry_at=_ts(event.next_retry_at) if event.next_retry_at else None,
                )
            case UsageThresholdReachedEvent():
                topic, key = topics.USAGE_THRESHOLD_REACHED, str(event.user_id)
                env, headers = self._envelope(topic)
                msg = pb.UsageThresholdReached(
                    envelope=env,
                    user_id=str(event.user_id),
                    billing_account_id=str(event.billing_account_id),
                    limit=event.limit,
                    percent=event.percent,
                    used=event.used,
                    allowed=event.allowed,
                    resets_at=_ts(event.resets_at),
                )
            case InvoiceIssuedEvent():
                topic, key = topics.INVOICE_ISSUED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg = pb.InvoiceIssued(
                    envelope=env,
                    billing_account_id=str(event.billing_account_id),
                    invoice_id=str(event.invoice_id),
                    number=event.number,
                    kind=event.kind,
                    total_paise=event.total,
                    pdf_object_key=event.pdf_object_key or "",
                )
        return EncodedEvent(topic, key, topic, msg.SerializeToString(), headers)


def decode_event(event_type: str, payload: bytes) -> tuple[envelope_pb2.Envelope, OutboxEvent]:
    """Inverse of EventCodec.encode; used by tests and by anything that reads our own topics."""
    match event_type:
        case topics.SUBSCRIPTION_CHANGED:
            m = pb.SubscriptionChanged.FromString(payload)
            return m.envelope, SubscriptionChangedEvent(
                billing_account_id=UUID(m.billing_account_id),
                owner_type=OwnerType(m.owner_type),
                owner_id=UUID(m.owner_id),
                subscription_id=UUID(m.subscription_id),
                plan_code=m.plan_code,
                status=SubStatus(m.status),
                previous_plan_code=m.previous_plan_code or None,
                previous_status=SubStatus(m.previous_status) if m.previous_status else None,
                current_period_end=_dt(m, "current_period_end"),
            )
        case topics.PAYMENT_FAILED:
            p = pb.PaymentFailed.FromString(payload)
            return p.envelope, PaymentFailedEvent(
                billing_account_id=UUID(p.billing_account_id),
                subscription_id=UUID(p.subscription_id),
                gateway_payment_id=p.gateway_payment_id,
                amount=Paise(p.amount_paise),
                reason=p.reason,
                next_retry_at=_dt(p, "next_retry_at"),
            )
        case topics.USAGE_THRESHOLD_REACHED:
            u = pb.UsageThresholdReached.FromString(payload)
            resets = _dt(u, "resets_at")
            assert resets is not None
            return u.envelope, UsageThresholdReachedEvent(
                user_id=UUID(u.user_id),
                billing_account_id=UUID(u.billing_account_id),
                limit=u.limit,
                percent=u.percent,
                used=u.used,
                allowed=u.allowed,
                resets_at=resets,
            )
        case topics.INVOICE_ISSUED:
            i = pb.InvoiceIssued.FromString(payload)
            return i.envelope, InvoiceIssuedEvent(
                billing_account_id=UUID(i.billing_account_id),
                invoice_id=UUID(i.invoice_id),
                number=i.number,
                kind=i.kind,
                total=Paise(i.total_paise),
                pdf_object_key=i.pdf_object_key or None,
            )
    raise ValueError(f"unknown event type: {event_type}")
