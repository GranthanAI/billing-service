"""Domain events <-> JSON. The only module that knows both."""

from dataclasses import dataclass
from uuid import UUID

from opentelemetry import trace

from billing.adapters.kafka import schemas
from billing.adapters.kafka.schemas import Envelope
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


@dataclass(frozen=True, slots=True)
class EncodedEvent:
    topic: str
    key: str
    event_type: str
    payload: bytes
    headers: dict[str, str]


def traceparent() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


class EventCodec:
    def __init__(self, ids: IdGenerator, clock: Clock) -> None:
        self._ids = ids
        self._clock = clock

    def _envelope(self, event_type: str) -> tuple[dict[str, object], dict[str, str]]:
        event_id = str(self._ids.new())
        correlation = correlation_id_var.get()
        fields: dict[str, object] = {
            "event_type": event_type,
            "event_id": event_id,
            "occurred_at": self._clock.now(),
            "correlation_id": correlation,
        }
        headers = {"event_id": event_id, "event_type": event_type}
        if correlation:
            headers["correlation_id"] = correlation
        tp = traceparent()
        if tp:
            headers["traceparent"] = tp
        return fields, headers

    def encode(self, event: OutboxEvent) -> EncodedEvent:
        match event:
            case SubscriptionChangedEvent():
                topic, key = topics.SUBSCRIPTION_CHANGED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg: Envelope = schemas.SubscriptionChangedMsg(
                    **env,  # type: ignore[arg-type]
                    billing_account_id=str(event.billing_account_id),
                    owner_type=event.owner_type.value,
                    owner_id=str(event.owner_id),
                    subscription_id=str(event.subscription_id),
                    plan_code=event.plan_code,
                    status=event.status.value,
                    previous_plan_code=event.previous_plan_code,
                    previous_status=event.previous_status.value if event.previous_status else None,
                    current_period_end=event.current_period_end,
                )
            case PaymentFailedEvent():
                topic, key = topics.PAYMENT_FAILED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg = schemas.PaymentFailedMsg(
                    **env,  # type: ignore[arg-type]
                    billing_account_id=str(event.billing_account_id),
                    subscription_id=str(event.subscription_id),
                    gateway_payment_id=event.gateway_payment_id,
                    amount_paise=int(event.amount),
                    reason=event.reason,
                    next_retry_at=event.next_retry_at,
                )
            case UsageThresholdReachedEvent():
                topic, key = topics.USAGE_THRESHOLD_REACHED, str(event.user_id)
                env, headers = self._envelope(topic)
                msg = schemas.UsageThresholdReachedMsg(
                    **env,  # type: ignore[arg-type]
                    user_id=str(event.user_id),
                    billing_account_id=str(event.billing_account_id),
                    limit=event.limit,
                    percent=event.percent,
                    used=event.used,
                    allowed=event.allowed,
                    resets_at=event.resets_at,
                )
            case InvoiceIssuedEvent():
                topic, key = topics.INVOICE_ISSUED, str(event.billing_account_id)
                env, headers = self._envelope(topic)
                msg = schemas.InvoiceIssuedMsg(
                    **env,  # type: ignore[arg-type]
                    billing_account_id=str(event.billing_account_id),
                    invoice_id=str(event.invoice_id),
                    number=event.number,
                    kind=event.kind,
                    total_paise=int(event.total),
                    pdf_object_key=event.pdf_object_key,
                )
        return EncodedEvent(topic, key, topic, msg.model_dump_json().encode(), headers)


def decode_event(event_type: str, payload: bytes) -> tuple[Envelope, OutboxEvent]:
    """Inverse of EventCodec.encode; used by tests and by anything that reads our own topics."""
    match event_type:
        case topics.SUBSCRIPTION_CHANGED:
            m = schemas.SubscriptionChangedMsg.model_validate_json(payload)
            return m, SubscriptionChangedEvent(
                billing_account_id=UUID(m.billing_account_id),
                owner_type=OwnerType(m.owner_type),
                owner_id=UUID(m.owner_id),
                subscription_id=UUID(m.subscription_id),
                plan_code=m.plan_code,
                status=SubStatus(m.status),
                previous_plan_code=m.previous_plan_code or None,
                previous_status=SubStatus(m.previous_status) if m.previous_status else None,
                current_period_end=m.current_period_end,
            )
        case topics.PAYMENT_FAILED:
            p = schemas.PaymentFailedMsg.model_validate_json(payload)
            return p, PaymentFailedEvent(
                billing_account_id=UUID(p.billing_account_id),
                subscription_id=UUID(p.subscription_id),
                gateway_payment_id=p.gateway_payment_id,
                amount=Paise(p.amount_paise),
                reason=p.reason,
                next_retry_at=p.next_retry_at,
            )
        case topics.USAGE_THRESHOLD_REACHED:
            u = schemas.UsageThresholdReachedMsg.model_validate_json(payload)
            return u, UsageThresholdReachedEvent(
                user_id=UUID(u.user_id),
                billing_account_id=UUID(u.billing_account_id),
                limit=u.limit,
                percent=u.percent,
                used=u.used,
                allowed=u.allowed,
                resets_at=u.resets_at,
            )
        case topics.INVOICE_ISSUED:
            i = schemas.InvoiceIssuedMsg.model_validate_json(payload)
            return i, InvoiceIssuedEvent(
                billing_account_id=UUID(i.billing_account_id),
                invoice_id=UUID(i.invoice_id),
                number=i.number,
                kind=i.kind,
                total=Paise(i.total_paise),
                pdf_object_key=i.pdf_object_key or None,
            )
    raise ValueError(f"unknown event type: {event_type}")
