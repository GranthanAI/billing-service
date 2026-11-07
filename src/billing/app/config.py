"""What the use cases need from settings, and the bundle of collaborators they share."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from billing.app.ports import Clock, IdGenerator, PaymentGateway, UnitOfWork
from billing.domain.catalog import Interval


@dataclass(frozen=True, slots=True)
class BillingConfig:
    razorpay_key_id: str
    supplier_state_code: str
    supplier_gstin: str = ""
    gst_registered: bool = True
    sac_code: str = "998319"
    invoice_series: str = "GA"
    credit_note_series: str = "CN"
    bill_of_supply_series: str = "BS"
    grace_days: int = 3
    org_grace_days: int = 15
    abandon_hours: int = 24
    total_count_month: int = 120
    total_count_year: int = 10
    brand_name: str = "GranthanAI"
    supplier_legal_name: str = "GranthanAI"
    supplier_address: str = ""

    def total_count(self, interval: Interval) -> int:
        return self.total_count_month if interval is Interval.MONTH else self.total_count_year


@dataclass(frozen=True, slots=True)
class BillingContext:
    """Shared by checkout, the webhook applier and the job handlers."""

    uow_factory: Callable[[], UnitOfWork]
    gateway: PaymentGateway
    clock: Clock
    ids: IdGenerator
    config: BillingConfig
    # Called after a commit that changed what a user may do; drops `ent:{uid}`.
    invalidate_entitlement: Callable[[UUID], Awaitable[None]]
    # An organisation's plan changed: clear every member's entitlement (after commit).
    invalidate_org: Callable[[UUID], Awaitable[None]] | None = None
