"""Row-shaped DTOs passed between use cases and repositories."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from billing.domain.catalog import Interval, ModelGroup, OwnerType, PlanVersion
from billing.domain.money import MicroInr, Paise
from billing.domain.subscription import SubscriptionState, SubStatus


class StaleRecord(Exception):
    """A compare-and-swap on lock_version lost the race."""


@dataclass(frozen=True, slots=True)
class BillingAccount:
    id: UUID
    owner_type: OwnerType
    owner_id: UUID
    billing_email: str | None
    legal_name: str | None = None
    gstin: str | None = None
    state_code: str | None = None
    gateway_customer_id: str | None = None
    blocked_at: datetime | None = None
    blocked_reason: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_at is not None


@dataclass(frozen=True, slots=True)
class PlanVersionRecord:
    plan: PlanVersion
    effective_from: datetime
    checksum: str
    razorpay_plan_month: str | None = None
    razorpay_plan_year: str | None = None
    retired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    id: UUID
    billing_account_id: UUID
    state: SubscriptionState
    seats: int = 1
    gateway: str = "razorpay"
    lock_version: int = 0
    created_at: datetime | None = None  # set by the repository on read


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    id: UUID
    owner_type: OwnerType
    owner_id: UUID
    plan_version_id: UUID
    reason: str
    starts_at: datetime
    expires_at: datetime
    granted_by: UUID
    ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UsageEventRecord:
    kind: str
    source_id: str
    occurred_at: datetime
    user_id: UUID
    billing_account_id: UUID
    plan_version_id: UUID
    billable_units: int
    cost_micro_inr: MicroInr
    fx_usd_inr: Any  # Decimal
    subscription_id: UUID | None = None
    model: str | None = None
    model_group: ModelGroup | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    aux_input_tokens: int = 0
    aux_output_tokens: int = 0
    price_paise: Paise = Paise(0)
    hold_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    id: UUID
    billing_account_id: UUID
    purpose: str
    gateway_payment_id: str
    amount_paise: Paise
    status: str
    captured_at: datetime
    raw: Mapping[str, Any]
    subscription_id: UUID | None = None
    gateway_order_id: str | None = None
    method: str | None = None
    fee_paise: Paise | None = None
    fee_gst_paise: Paise | None = None


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    id: UUID
    number: str
    kind: str  # tax_invoice | credit_note
    billing_account_id: UUID
    issued_at: datetime
    customer_state_code: str
    supplier_state_code: str
    taxable_paise: Paise
    cgst_paise: Paise
    sgst_paise: Paise
    igst_paise: Paise
    total_paise: Paise
    status: str  # issued | paid | void
    original_invoice_id: UUID | None = None
    payment_id: UUID | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    customer_name: str | None = None
    customer_gstin: str | None = None
    pdf_object_key: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLineRecord:
    invoice_id: UUID
    line_no: int
    description: str
    sac_code: str
    quantity: int
    unit_price_paise: Paise
    taxable_paise: Paise


@dataclass(frozen=True, slots=True)
class SubscriptionHistoryEntry:
    subscription_id: UUID
    from_status: SubStatus | None
    to_status: SubStatus
    trigger: str
    cause_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RefundRecord:
    id: UUID
    payment_id: UUID
    gateway_refund_id: str
    amount_paise: Paise
    reason: str
    status: str


class ChangeKind(StrEnum):
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    RESUME = "resume"


class ChangeStatus(StrEnum):
    PENDING = "pending"
    AUTHORISED = "authorised"
    APPLIED = "applied"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """A plan change (or resume) waiting for its new mandate."""

    id: UUID
    subscription_id: UUID
    kind: ChangeKind
    new_plan_version_id: UUID
    new_interval: Interval
    gateway_subscription_id: str
    status: ChangeStatus
    refund_payment_id: UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: int
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    attempts: int
    received_at: datetime


# --- idempotency ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class New:
    pass


@dataclass(frozen=True, slots=True)
class Replay:
    status: int
    body: Any


@dataclass(frozen=True, slots=True)
class Mismatch:
    pass


@dataclass(frozen=True, slots=True)
class InProgress:
    pass


Begin = New | Replay | Mismatch | InProgress


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
