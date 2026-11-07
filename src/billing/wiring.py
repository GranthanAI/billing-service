"""Builds the Phase 6 collaborators from settings; shared by the API, the worker and tests."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from billing.adapters.storage import InMemoryStorage, S3Storage
from billing.app.account_views import AccountViews
from billing.app.admin import AdminService
from billing.app.config import BillingConfig, BillingContext
from billing.app.ports import Clock, IdGenerator, ObjectStorage, PaymentGateway, UnitOfWork
from billing.app.subscriptions import SubscriptionService
from billing.app.webhooks import EventApplier
from billing.settings import Settings


def config_from_settings(s: Settings) -> BillingConfig:
    return BillingConfig(
        razorpay_key_id=s.razorpay_key_id,
        supplier_state_code=s.supplier_state_code,
        supplier_gstin=s.supplier_gstin,
        gst_registered=s.gst_registered,
        sac_code=s.invoice_sac_code,
        supplier_legal_name=s.supplier_legal_name,
        supplier_address=s.supplier_address,
        invoice_series=s.invoice_series,
        credit_note_series=s.credit_note_series,
        bill_of_supply_series=s.bill_of_supply_series,
        grace_days=s.grace_days,
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
) -> BillingContext:
    return BillingContext(uow_factory, gateway, clock, ids, config, invalidate, invalidate_org)


def install_billing(
    app: FastAPI,
    bctx: BillingContext,
    *,
    views: AccountViews | None = None,
    admin: AdminService | None = None,
    orgs: Any = None,
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
    if orgs is not None:
        from billing.app.org_billing import OrgBilling

        app.state.org_billing = OrgBilling(bctx)
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
