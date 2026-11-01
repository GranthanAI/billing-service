"""Ports (Protocols) between use cases and adapters.

PROVISIONAL for the gateway and counter ports; the repository ports below are the Phase 2
contract. All methods are async and take or return domain types or `app/records.py` DTOs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from billing.app.records import (
    Begin,
    BillingAccount,
    InvoiceLineRecord,
    InvoiceRecord,
    OverrideRecord,
    PaymentRecord,
    PlanVersionRecord,
    SubscriptionHistoryEntry,
    SubscriptionRecord,
    UsageEventRecord,
)
from billing.domain.catalog import DenyReason, ModelGroup, OrgVariant, OwnerType, ReserveKind
from billing.domain.ledger import LedgerTransaction
from billing.domain.money import Paise
from billing.domain.rating import PriceBook


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware, UTC."""
        ...


class IdGenerator(Protocol):
    def new(self) -> UUID: ...


# --- payment gateway -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GatewayPlan:
    id: str


@dataclass(frozen=True, slots=True)
class GatewayPlanSpec:
    """What ensure_plan needs. `notes` carries plan_code, version, interval and checksum."""

    name: str
    period: str  # monthly | yearly
    interval: int
    amount: Paise
    currency: str
    notes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GatewaySubscription:
    id: str
    status: str
    plan_id: str
    current_start: datetime | None
    current_end: datetime | None
    notes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GatewayOrder:
    id: str
    amount: Paise
    notes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GatewayPayment:
    id: str
    amount: Paise
    status: str
    method: str | None
    fee: Paise | None
    fee_gst: Paise | None
    order_id: str | None
    subscription_id: str | None
    captured_at: datetime | None


@dataclass(frozen=True, slots=True)
class GatewayRefund:
    id: str
    payment_id: str
    amount: Paise
    status: str


class PlanGateway(Protocol):
    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        """Idempotent: returns the id of an existing plan with the same notes.checksum and
        notes.interval, and creates one only when there is none."""
        ...


class PaymentGateway(PlanGateway, Protocol):
    async def create_plan(
        self, *, name: str, amount: Paise, interval: str, notes: Mapping[str, str]
    ) -> GatewayPlan: ...

    async def create_subscription(
        self, plan_id: str, *, start_at: datetime | None, notes: Mapping[str, str]
    ) -> GatewaySubscription: ...

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription: ...

    async def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool) -> None: ...

    async def create_order(self, amount: Paise, *, notes: Mapping[str, str]) -> GatewayOrder: ...

    async def fetch_payment(self, payment_id: str) -> GatewayPayment: ...

    async def refund_payment(
        self, payment_id: str, amount: Paise, notes: Mapping[str, str]
    ) -> GatewayRefund: ...

    def verify_checkout_signature(
        self, payment_id: str, subscription_id: str, signature: str
    ) -> bool: ...

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool: ...


# --- Valkey counters -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReserveArgs:
    uid: UUID
    account: UUID | None
    plan_version_id: UUID
    idem_key: str
    kind: ReserveKind
    mode: str  # "plan" | "payg"
    field: str
    limit: int
    best_limit: int
    price_paise: Paise
    hold_id: str
    now_unix: int
    hold_ttl: int
    counter_ttl: int
    no_credit_fast: int
    counter_key: str


@dataclass(frozen=True, slots=True)
class ReserveResult:
    allowed: bool
    deny_reason: DenyReason | None
    group_or_field: str | None
    hold_id: str | None
    price: Paise
    downgraded: bool


@dataclass(frozen=True, slots=True)
class Hold:
    hold_id: str
    user_id: UUID
    kind: ReserveKind
    group: ModelGroup | None
    mode: str
    price: Paise
    day_key: str
    billing_account_id: UUID | None
    plan_version_id: UUID
    expires_at: datetime


class Counters(Protocol):
    async def reserve(self, args: ReserveArgs) -> ReserveResult: ...

    async def commit(
        self,
        user_id: UUID,
        hold_id: str,
        charged: Paise,
        source_id: str,
        group: ModelGroup | None,
        occurred_at: datetime,
    ) -> str: ...

    async def release(self, user_id: UUID, hold_id: str, reason: str) -> str: ...

    async def peek_hold(self, user_id: UUID, hold_id: str) -> Hold | None: ...


# --- storage ---------------------------------------------------------------------------


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def presigned_get(self, key: str, expires_s: int) -> str: ...


# --- repositories ----------------------------------------------------------------------


class AccountRepo(Protocol):
    async def get_by_owner(
        self, owner_type: OwnerType, owner_id: UUID
    ) -> BillingAccount | None: ...

    async def get_or_create(
        self, owner_type: OwnerType, owner_id: UUID, email: str
    ) -> BillingAccount: ...

    async def set_state_code(self, account_id: UUID, code: str) -> None: ...

    async def set_gstin(
        self, account_id: UUID, gstin: str | None, legal_name: str | None
    ) -> None: ...


class PlanRepo(Protocol):
    async def by_id(self, plan_version_id: UUID) -> PlanVersionRecord | None: ...

    async def current(
        self, plan_code: str, variant: OrgVariant | None
    ) -> PlanVersionRecord | None: ...

    async def all_current(self) -> Sequence[PlanVersionRecord]: ...

    async def insert_version(self, record: PlanVersionRecord) -> None: ...

    async def retire(self, plan_version_id: UUID, at: datetime) -> None: ...

    async def set_gateway_plan_ids(
        self, plan_version_id: UUID, month: str | None, year: str | None
    ) -> None: ...


class PricingRepo(Protocol):
    """Implemented in Phase 3."""

    async def price_book_at(self, ts: datetime) -> PriceBook: ...

    async def fx_at(self, ts: datetime) -> Decimal: ...

    async def add_model_price(
        self,
        *,
        model: str,
        provider: str,
        model_group: ModelGroup,
        input_usd_per_m: Decimal,
        cached_input_usd_per_m: Decimal | None,
        output_usd_per_m: Decimal,
        effective_from: datetime,
    ) -> None: ...

    async def add_fx_rate(self, effective_from: date, usd_inr: Decimal, source: str) -> None: ...


class SubscriptionRepo(Protocol):
    async def get(self, subscription_id: UUID) -> SubscriptionRecord | None: ...

    async def lock(self, subscription_id: UUID) -> SubscriptionRecord | None:
        """SELECT ... FOR UPDATE."""
        ...

    async def lock_by_gateway_id(self, gateway_subscription_id: str) -> SubscriptionRecord | None:
        """Matches the current or the next mandate; locks the row."""
        ...

    async def live_for_account(self, billing_account_id: UUID) -> SubscriptionRecord | None: ...

    async def count_live_for_plan(self, plan_code: str) -> int:
        """Subscriptions on any version of the plan that are active, past_due, suspended or
        cancelling."""
        ...

    async def insert(self, record: SubscriptionRecord) -> None: ...

    async def save(self, record: SubscriptionRecord) -> SubscriptionRecord:
        """Compare-and-swap on lock_version; raises StaleRecord on a mismatch."""
        ...

    async def append_history(self, entry: SubscriptionHistoryEntry) -> None: ...


class OverrideRepo(Protocol):
    async def active_for(
        self, owner_type: OwnerType, owner_id: UUID, now: datetime
    ) -> Sequence[OverrideRecord]: ...

    async def insert(self, record: OverrideRecord) -> None: ...

    async def end(self, override_id: UUID, now: datetime) -> None: ...


class UsageRepo(Protocol):
    async def insert(self, record: UsageEventRecord) -> bool:
        """False when the (kind, source_id, occurred_at) key already exists."""
        ...

    async def counts_for_day(self, user_id: UUID, ist_day: date) -> dict[ModelGroup, int]: ...

    async def rollup_day(self, ist_day: date) -> int:
        """Recompute that IST day's usage_daily rows; returns the number of rows written."""
        ...


class PaymentRepo(Protocol):
    async def insert(self, record: PaymentRecord) -> bool: ...

    async def by_gateway_id(self, gateway_payment_id: str) -> PaymentRecord | None: ...

    async def mark_refunded(self, payment_id: UUID, total_refunded: Paise) -> None: ...


class InvoiceRepo(Protocol):
    async def allocate_number(self, series: str, financial_year: str) -> str: ...

    async def insert(self, invoice: InvoiceRecord, lines: Sequence[InvoiceLineRecord]) -> None: ...

    async def set_pdf(self, invoice_id: UUID, object_key: str) -> None: ...

    async def mark_paid(self, invoice_id: UUID) -> None: ...


class LedgerRepo(Protocol):
    async def post(
        self, tx: LedgerTransaction, ref_type: str, ref_id: str, occurred_at: datetime
    ) -> bool:
        """False when the idempotency key already exists."""
        ...

    async def balance(self, code: str) -> int: ...

    async def lock_balance(self, code: str) -> int: ...


class Publisher(Protocol):
    async def enqueue(
        self,
        topic: str,
        key: str,
        event_type: str,
        payload: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Write to the outbox inside the current unit of work."""
        ...


class ProcessedEventRepo(Protocol):
    async def claim(self, consumer: str, event_id: str) -> bool:
        """True if this call claimed the event, False if it was already processed."""
        ...


class IdempotencyRepo(Protocol):
    async def begin(self, scope: str, key: str, request_hash: str, lock_seconds: int) -> Begin: ...

    async def complete(self, scope: str, key: str, status: int, body: Any) -> None: ...


class WebhookInboxRepo(Protocol):
    async def store(
        self, gateway: str, event_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> bool: ...


class JobRepo(Protocol):
    async def enqueue(
        self, kind: str, payload: Mapping[str, Any], run_at: datetime, dedupe_key: str | None
    ) -> bool: ...


class AuditRepo(Protocol):
    async def record(
        self,
        actor_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        reason: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
    ) -> None: ...


class UnitOfWork(Protocol):
    @property
    def accounts(self) -> AccountRepo: ...

    @property
    def plans(self) -> PlanRepo: ...

    @property
    def pricing(self) -> PricingRepo: ...

    @property
    def subscriptions(self) -> SubscriptionRepo: ...

    @property
    def overrides(self) -> OverrideRepo: ...

    @property
    def usage(self) -> UsageRepo: ...

    @property
    def payments(self) -> PaymentRepo: ...

    @property
    def invoices(self) -> InvoiceRepo: ...

    @property
    def ledger(self) -> LedgerRepo: ...

    @property
    def publisher(self) -> Publisher: ...

    @property
    def processed(self) -> ProcessedEventRepo: ...

    @property
    def idempotency(self) -> IdempotencyRepo: ...

    @property
    def inbox(self) -> WebhookInboxRepo: ...

    @property
    def jobs(self) -> JobRepo: ...

    @property
    def audit(self) -> AuditRepo: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
