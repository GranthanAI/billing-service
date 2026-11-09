"""Builds the Phase 6 collaborators from settings; shared by the API, the worker and tests."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from billing.adapters.catalog.yaml_loader import CatalogError, load_catalog
from billing.adapters.storage import InMemoryStorage, S3Storage
from billing.app.account_views import AccountViews
from billing.app.admin import AdminService
from billing.app.catalog_spec import PaygSpec
from billing.app.config import BillingConfig, BillingContext
from billing.app.ports import Clock, IdGenerator, ObjectStorage, PaymentGateway, UnitOfWork
from billing.app.subscriptions import SubscriptionService
from billing.app.webhooks import EventApplier
from billing.domain.catalog import PaygPrices
from billing.domain.money import Paise
from billing.settings import Settings

SIM_INVOICE_SERIES, SIM_BILL_OF_SUPPLY_SERIES, SIM_CREDIT_NOTE_SERIES = "SI", "SB", "SC"
SIM_FAST_POLL_AFTER_SECONDS = 30


def _payg_spec(s: Settings) -> PaygSpec | None:
    try:
        return load_catalog(s.plans_file).payg
    except (CatalogError, OSError):
        return None


def payg_prices(s: Settings) -> PaygPrices | None:
    """Unit prices for the credits path, or None while PAYG_ENABLED is off (v1 launch)."""
    spec = _payg_spec(s) if s.payg_enabled else None
    if spec is None:
        return None
    return PaygPrices(
        unit_prices={k: Paise(v) for k, v in spec.unit_prices.items()},
        long_message_tokens=spec.long_message_tokens,
        no_credit_fast_per_day=spec.no_credit_fast_per_day,
    )


def config_from_settings(s: Settings) -> BillingConfig:
    spec = _payg_spec(s)
    wallet: dict[str, Any] = (
        {
            "topup_packs": spec.topup_packs,
            "org_min_topup": spec.org_min_topup,
            "credit_validity_months": spec.credit_validity_months,
            "storage_gb_month_paise": spec.storage_over_10gb_per_gb_month,
        }
        if spec
        else {}
    )
    sim = s.payment_simulation
    return BillingConfig(
        **wallet,
        payg_enabled=s.payg_enabled,
        razorpay_key_id=s.razorpay_key_id,
        supplier_state_code=s.supplier_state_code,
        supplier_gstin=s.supplier_gstin,
        gst_registered=s.gst_registered,
        sac_code=s.invoice_sac_code,
        supplier_legal_name=s.supplier_legal_name,
        supplier_address=s.supplier_address,
        # simulated invoices never consume the real GST series (plan 5.6)
        invoice_series=SIM_INVOICE_SERIES if sim else s.invoice_series,
        credit_note_series=SIM_CREDIT_NOTE_SERIES if sim else s.credit_note_series,
        bill_of_supply_series=SIM_BILL_OF_SUPPLY_SERIES if sim else s.bill_of_supply_series,
        grace_days=s.grace_days,
        grace_period_seconds=s.grace_period_seconds,
        poll_after_seconds=SIM_FAST_POLL_AFTER_SECONDS if sim and s.sim_fast_jobs else 7200,
        simulation=sim,
        email_enabled=s.email_enabled,
        frontend_origin=s.frontend_origin,
        support_email=s.support_email,
        org_grace_days=s.org_grace_days,
        abandon_hours=s.checkout_abandon_hours,
        total_count_month=s.razorpay_total_count_month,
        total_count_year=s.razorpay_total_count_year,
    )


def make_storage(s: Settings) -> ObjectStorage:
    """The S3-compatible bucket when it is configured, otherwise an in-memory stand-in (local
    development: PDFs then live only as long as the process)."""
    if s.object_store_bucket and s.object_store_access_key:
        return S3Storage(
            endpoint_url=s.object_store_endpoint,
            bucket=s.object_store_bucket,
            access_key=s.object_store_access_key,
            secret_key=s.object_store_secret_key,
            region=s.object_store_region,
        )
    return InMemoryStorage()


def make_context(
    config: BillingConfig,
    uow_factory: Callable[[], UnitOfWork],
    gateway: PaymentGateway,
    clock: Clock,
    ids: IdGenerator,
    invalidate: Callable[[UUID], Awaitable[None]],
    invalidate_org: Callable[[UUID], Awaitable[object]] | None = None,
    credit_wallet: Callable[[UUID, int], Awaitable[object]] | None = None,
) -> BillingContext:
    return BillingContext(
        uow_factory, gateway, clock, ids, config, invalidate, invalidate_org, credit_wallet
    )


def install_billing(
    app: FastAPI,
    bctx: BillingContext,
    *,
    views: AccountViews | None = None,
    admin: AdminService | None = None,
    orgs: Any = None,
    counters: Any = None,
    org_service_token: str = "",
) -> EventApplier:
    """Attach what the REST routes and the idempotency middleware read from app.state."""
    applier = EventApplier(bctx)
    app.state.uow_factory = bctx.uow_factory
    app.state.gateway = bctx.gateway
    app.state.subscriptions = SubscriptionService(bctx, applier)
    if views is not None:
        app.state.views = views
    if admin is not None:
        app.state.admin = admin
    from billing.app.wallet import WalletService

    app.state.wallet = WalletService(bctx, counters)
    if orgs is not None:
        from billing.app.org_billing import OrgBilling
        from billing.app.org_topups import OrgTopups

        app.state.org_billing = OrgBilling(bctx)
        app.state.org_topups = OrgTopups(bctx)
        app.state.org_directory = orgs
        app.state.org_service_token = org_service_token
    return applier


def make_org_directory(s: Settings, client: Any) -> Any:
    """org-service over HTTP, cached in Valkey; with no URL configured nobody belongs to an org."""
    from billing.adapters.orgs import CachedOrgDirectory, HttpOrgDirectory, NoOrgDirectory

    if not s.org_service_url:
        return NoOrgDirectory()
    return CachedOrgDirectory(
        HttpOrgDirectory(s.org_service_url, s.org_service_token),
        client,
        s.org_directory_ttl_seconds,
    )
