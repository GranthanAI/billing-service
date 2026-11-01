"""Ports (Protocols) between use cases and adapters.

PROVISIONAL: signatures and DTOs here are a first draft. Phases 2 to 7 will extend them as
the adapters and use cases that need them are built.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from billing.domain.catalog import ModelGroup, ReserveKind
from billing.domain.money import Paise


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware, UTC."""
        ...


# --- payment gateway -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GatewayPlan:
    id: str


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


class PaymentGateway(Protocol):
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
    deny_code: str | None
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


# --- events, storage, unit of work -----------------------------------------------------


class Publisher(Protocol):
    async def enqueue(self, topic: str, key: str, event: object) -> None:
        """Write to the outbox inside the current unit of work."""
        ...


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def presigned_get(self, key: str, expires_s: int) -> str: ...


class SubscriptionRepo(Protocol):
    async def lock_by_gateway_id(self, gateway_subscription_id: str) -> object | None: ...


class ProcessedEventsRepo(Protocol):
    async def claim(self, consumer: str, event_id: str) -> bool:
        """True if this call claimed the event, False if it was already processed."""
        ...


class UnitOfWork(Protocol):
    subs: SubscriptionRepo
    processed: ProcessedEventsRepo
    publisher: Publisher

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
