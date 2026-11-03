"""Ports (Protocols) between use cases and adapters.

PROVISIONAL for the gateway and counter ports; the repository ports below are the Phase 2
contract. All methods are async and take or return domain types or `app/records.py` DTOs.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from billing.app.catalog_spec import ModelEntry
from billing.app.events import OutboxEvent
from billing.app.records import (
    Begin,
    BillingAccount,
    ChangeRecord,
    ChangeStatus,
    InboxRow,
    InvoiceLineRecord,
    InvoiceRecord,
    Job,
    JobStatus,
    OverrideRecord,
    PaymentRecord,
    PlanVersionRecord,
    RefundRecord,
    SubscriptionHistoryEntry,
    SubscriptionRecord,
    UsageEventRecord,
)
from billing.domain.catalog import (
    DenyReason,
    Interval,
    ModelGroup,
    OrgVariant,
    OwnerType,
    ReserveKind,
)
from billing.domain.entitlements import Entitlement
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
    plan_id: str
    status: str
    current_start: datetime | None
    current_end: datetime | None
    charge_at: datetime | None
    start_at: datetime | None
    paid_count: int
    notes: Mapping[str, str]
    short_url: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayPayment:
    id: str
    amount: Paise
    currency: str
    status: str
    method: str | None
    fee: Paise | None
    tax: Paise | None  # the GST Razorpay charged on its fee
    order_id: str | None
    invoice_id: str | None
    subscription_id: str | None  # Razorpay payments carry no subscription id; see the adapter
    created_at: datetime
    notes: Mapping[str, str]
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GatewayRefund:
    id: str
    payment_id: str
    amount: Paise
    status: str
    notes: Mapping[str, str]


class PlanGateway(Protocol):
    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        """Idempotent: returns the id of an existing plan with the same notes.checksum and
        notes.interval, and creates one only when there is none."""
        ...


class PaymentGateway(PlanGateway, Protocol):
    """Raises GatewayRejected on a 4xx and GatewayUnavailable once retries are exhausted."""

    async def create_subscription(
        self,
        plan_id: str,
        *,
        total_count: int,
        start_at: datetime | None,
        notes: Mapping[str, str],
        lookup_first: bool = False,
    ) -> GatewaySubscription:
        """Idempotent on notes["billing_ref"]: a retry after a timeout finds the subscription
        it already created. `lookup_first` makes the first attempt look too (crash recovery)."""
        ...

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription: ...

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> GatewaySubscription: ...

    async def fetch_payment(self, payment_id: str) -> GatewayPayment: ...

    async def list_subscription_payments(self, subscription_id: str) -> list[GatewayPayment]:
        """Captured payments of a subscription, newest first (through its invoices)."""
        ...

    async def refund_payment(
        self, payment_id: str, amount: Paise, *, notes: Mapping[str, str]
    ) -> GatewayRefund:
        """Idempotent on notes["refund_key"]."""
        ...

    async def fetch_refund(self, refund_id: str) -> GatewayRefund: ...

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
    period: str  # "day" | "month": which IST counter hash the adapter picks (and its TTL)
    now: datetime
    mode: str  # "plan" | "payg"
    field: str
    limit: int
    best_limit: int
    price_paise: Paise
    hold_id: str
    hold_ttl: int
    no_credit_fast: int


@dataclass(frozen=True, slots=True)
class ReserveResult:
    allowed: bool
    deny_reason: DenyReason | None
    field: str | None  # the counter field charged: a model group, "youtube" or "manim"
    hold_id: str | None
    price: Paise
    downgraded: bool
    total: int  # the counter total after this reservation (0 on a deny)


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
    expires_at: datetime | None


class Counters(Protocol):
    async def reserve(self, args: ReserveArgs) -> ReserveResult: ...

    async def commit(
        self,
        user_id: UUID,
        hold_id: str,
        charged: Paise,
        source_id: str,
        kind: ReserveKind,
        field: str,
        occurred_at: datetime,
    ) -> str:
        """`OK`, or `DUP` when this source_id was already committed."""
        ...

    async def release(self, user_id: UUID, hold_id: str, reason: str) -> bool:
        """False when the hold is already gone (committed or released)."""
        ...

    async def peek_hold(self, user_id: UUID, hold_id: str) -> Hold | None: ...

    async def day_counts(self, user_id: UUID, day: date) -> dict[str, int]: ...

    async def due_holds(self, now_unix: int, limit: int) -> list[tuple[UUID, str]]: ...

    async def open_holds(self) -> int: ...


class EntitlementCache(Protocol):
    async def get(self, user_id: UUID) -> Entitlement | None: ...

    async def set(self, user_id: UUID, entitlement: Entitlement, ttl_seconds: int) -> None: ...

    async def delete(self, user_id: UUID) -> None:
        """Also announces the invalidation to other replicas."""
        ...

    def invalidations(self) -> AsyncIterator[UUID | None]:
        """User ids other replicas invalidated; None whenever the subscription is (re)made."""
        ...


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

    async def get(self, account_id: UUID) -> BillingAccount | None: ...

    async def update_checkout(
        self,
        account_id: UUID,
        *,
        state_code: str,
        email: str,
        legal_name: str | None,
        gstin: str | None,
    ) -> None:
        """Checkout always refreshes state and email; name and GSTIN only when given."""
        ...

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

    async def list_models(self) -> list[ModelEntry]: ...

    async def sync_models(self, entries: Sequence[ModelEntry]) -> int:
        """Make the models table equal `entries`; returns rows added, changed or removed."""
        ...

    async def set_gateway_plan_ids(
        self, plan_version_id: UUID, month: str | None, year: str | None
    ) -> None: ...


class PricingRepo(Protocol):
    """Implemented in Phase 3."""

    async def price_book_at(self, ts: datetime) -> PriceBook: ...

    async def fx_at(self, ts: datetime) -> Decimal: ...

    async def model_groups_at(self, ts: datetime) -> dict[str, ModelGroup]:
        """model id -> its group, for the prices effective at `ts`."""
        ...

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

    async def find_reusable_incomplete(
        self,
        billing_account_id: UUID,
        plan_version_id: UUID,
        interval: Interval,
        created_after: datetime,
    ) -> SubscriptionRecord | None: ...

    async def list_incomplete_before(
        self, cutoff: datetime, limit: int
    ) -> Sequence[SubscriptionRecord]:
        """`incomplete` subscriptions created before `cutoff`, oldest first."""
        ...

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

    async def counts_by_user_for_day(self, ist_day: date) -> dict[UUID, dict[str, int]]:
        """Committed usage of one IST day per user, by counter field (a model group, or manim)."""
        ...

    async def youtube_counts_for_month(self, year: int, month: int) -> dict[UUID, int]: ...

    async def rollup_day(self, ist_day: date) -> int:
        """Recompute that IST day's usage_daily rows; returns the number of rows written."""
        ...


class PaymentRepo(Protocol):
    async def insert(self, record: PaymentRecord) -> bool: ...

    async def by_gateway_id(self, gateway_payment_id: str) -> PaymentRecord | None: ...

    async def get(self, payment_id: UUID) -> PaymentRecord | None: ...

    async def latest_for_subscription(self, subscription_id: UUID) -> PaymentRecord | None:
        """The most recent captured (or part-refunded) payment of a subscription."""
        ...

    async def mark_refunded(self, payment_id: UUID, total_refunded: Paise) -> None: ...


class RefundRepo(Protocol):
    async def insert(self, record: RefundRecord) -> bool:
        """False when that gateway refund is already recorded."""
        ...

    async def by_gateway_id(self, gateway_refund_id: str) -> RefundRecord | None: ...

    async def set_status(self, gateway_refund_id: str, status: str) -> bool:
        """False when we have no such refund."""
        ...

    async def total_for_payment(self, payment_id: UUID) -> Paise:
        """Sum of the refunds recorded for a payment, whatever their status except failed."""
        ...


class ChangeRepo(Protocol):
    async def insert(self, record: ChangeRecord) -> None: ...

    async def get(self, change_id: UUID) -> ChangeRecord | None: ...

    async def by_gateway_id(self, gateway_subscription_id: str) -> ChangeRecord | None: ...

    async def open_for_subscription(self, subscription_id: UUID) -> ChangeRecord | None:
        """The one `pending` or `authorised` change, if any."""
        ...

    async def set_status(self, change_id: UUID, status: ChangeStatus) -> None: ...

    async def list_pending_before(self, cutoff: datetime, limit: int) -> Sequence[ChangeRecord]: ...


class InvoiceRepo(Protocol):
    async def allocate_number(self, series: str, financial_year: str) -> str: ...

    async def insert(self, invoice: InvoiceRecord, lines: Sequence[InvoiceLineRecord]) -> None: ...

    async def set_pdf(self, invoice_id: UUID, object_key: str) -> None: ...

    async def mark_paid(self, invoice_id: UUID) -> None: ...

    async def get(self, invoice_id: UUID) -> InvoiceRecord | None: ...

    async def by_payment(self, payment_id: UUID) -> InvoiceRecord | None:
        """The tax invoice (or bill of supply) issued for a payment; never a credit note."""
        ...


class LedgerRepo(Protocol):
    async def post(
        self, tx: LedgerTransaction, ref_type: str, ref_id: str, occurred_at: datetime
    ) -> bool:
        """False when the idempotency key already exists."""
        ...

    async def balance(self, code: str) -> int: ...

    async def lock_balance(self, code: str) -> int: ...


class Publisher(Protocol):
    async def enqueue(self, event: OutboxEvent) -> None:
        """Write to the outbox inside the current unit of work."""
        ...


class ProcessedEventRepo(Protocol):
    async def claim(self, consumer: str, event_id: str) -> bool:
        """True if this call claimed the event, False if it was already processed."""
        ...


class IdempotencyRepo(Protocol):
    async def begin(self, scope: str, key: str, request_hash: str, lock_seconds: int) -> Begin: ...

    async def complete(self, scope: str, key: str, status: int, body: Any) -> None: ...

    async def release(self, scope: str, key: str) -> None:
        """Forget an unfinished key (the handler failed with a 5xx) so the client can retry."""
        ...


class WebhookInboxRepo(Protocol):
    async def store(
        self, gateway: str, event_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> bool: ...

    async def claim_due(self, limit: int, lease_seconds: int, now: datetime) -> list[InboxRow]:
        """`received` rows due at `now` (FOR UPDATE SKIP LOCKED); their next_try_at moves
        `lease_seconds` ahead, so another replica leaves them alone while this one works."""
        ...

    async def mark(self, row_id: int, status: str) -> None:
        """`processed` or `ignored`."""
        ...

    async def record_failure(
        self, row_id: int, error: str, now: datetime, max_attempts: int
    ) -> str:
        """attempts += 1; retry at now + min(30 s * 2**(attempts-1), 1 h); `failed` once
        attempts reach max_attempts. Returns the new status."""
        ...


class JobRepo(Protocol):
    async def enqueue(
        self, kind: str, payload: Mapping[str, Any], run_at: datetime, dedupe_key: str | None
    ) -> bool: ...

    async def claim(self, limit: int, lock_seconds: int, now: datetime) -> list[Job]:
        """Queued jobs due at `now` become running and locked (FOR UPDATE SKIP LOCKED)."""
        ...

    async def complete(self, job_id: int) -> None: ...

    async def fail(self, job_id: int, error: str, now: datetime) -> JobStatus:
        """Requeue with run_at = now + min(2**attempts, 3600) s, or `dead` at max_attempts."""
        ...

    async def reap(self, now: datetime) -> int:
        """Requeue running jobs whose lock expired; returns how many."""
        ...


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
    def refunds(self) -> RefundRepo: ...

    @property
    def changes(self) -> ChangeRepo: ...

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

    def after_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run `callback` once the transaction has committed; dropped on rollback. A callback
        that raises is logged and does not undo the commit."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
