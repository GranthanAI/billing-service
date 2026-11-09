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
    grace_period_seconds: int | None = None  # GRACE_PERIOD_SECONDS: overrides grace_days
    poll_after_seconds: int = 7200  # subscription.poll looks at periods ended this long ago
    simulation: bool = False  # PAYMENT_SIMULATION
    email_enabled: bool = True  # EMAIL_ENABLED: false enqueues no emails at all
    frontend_origin: str = "http://localhost:5173"  # emails link here, never to a token URL
    support_email: str = "support@granthan.ai"
    org_grace_days: int = 15
    abandon_hours: int = 24
    total_count_month: int = 120
    total_count_year: int = 10
    brand_name: str = "GranthanAI"
    supplier_legal_name: str = "GranthanAI"
    supplier_address: str = ""
    # Pay as you Go (from plans.yaml): (pack code, price in paise incl. GST), the smallest
    # organisation top-up, and how long credits last
    topup_packs: tuple[tuple[str, int], ...] = (
        ("ind_199", 19900),
        ("ind_499", 49900),
        ("ind_999", 99900),
        ("ind_1999", 199900),
    )
    org_min_topup: int = 500000
    credit_validity_months: int = 12
    storage_gb_month_paise: int = 1000  # per GB over 10 GiB per month, GST included
    # PAYG_ENABLED. False (the v1 launch) closes Pay as you Go completely: no top-up or settings
    # routes, no PAYG grant, no wallet credit.
    # Turning it on needs the CA sign-off, not code.
    payg_enabled: bool = False

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
    invalidate_org: Callable[[UUID], Awaitable[object]] | None = None
    # After a top-up commits: add the taxable paise to the Valkey wallet mirror (HINCRBY).
    credit_wallet: Callable[[UUID, int], Awaitable[object]] | None = None
