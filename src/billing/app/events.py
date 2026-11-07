"""Events billing publishes, as plain domain data. Protobuf lives only in adapters/kafka."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from billing.domain.catalog import OwnerType
from billing.domain.money import Paise
from billing.domain.subscription import SubStatus


@dataclass(frozen=True, slots=True)
class SubscriptionChangedEvent:
    billing_account_id: UUID
    owner_type: OwnerType
    owner_id: UUID
    subscription_id: UUID
    plan_code: str
    status: SubStatus
    previous_plan_code: str | None
    previous_status: SubStatus | None
    current_period_end: datetime | None


@dataclass(frozen=True, slots=True)
class PaymentFailedEvent:
    billing_account_id: UUID
    subscription_id: UUID
    gateway_payment_id: str
    amount: Paise
    reason: str
    next_retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class RenewalUpcomingEvent:
    """A renewal is 24 to 48 hours away (the bank sends the RBI pre-debit notice itself)."""

    billing_account_id: UUID
    owner_id: UUID
    plan_code: str
    amount: Paise
    charge_at: datetime


@dataclass(frozen=True, slots=True)
class PersonalPlanRedundantEvent:
    """A user joined an organisation whose plan is as good as the personal plan they pay for."""

    user_id: UUID
    billing_account_id: UUID
    personal_plan_code: str
    org_id: UUID
    org_plan_code: str


@dataclass(frozen=True, slots=True)
class UsageThresholdReachedEvent:
    user_id: UUID
    billing_account_id: UUID
    limit: str
    percent: int
    used: int
    allowed: int
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class InvoiceIssuedEvent:
    billing_account_id: UUID
    invoice_id: UUID
    number: str
    kind: str
    total: Paise
    pdf_object_key: str | None


OutboxEvent = (
    SubscriptionChangedEvent
    | PaymentFailedEvent
    | RenewalUpcomingEvent
    | PersonalPlanRedundantEvent
    | UsageThresholdReachedEvent
    | InvoiceIssuedEvent
)
