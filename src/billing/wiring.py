"""Builds the Phase 6 collaborators from settings; shared by the API, the worker and tests."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import FastAPI

from billing.app.config import BillingConfig, BillingContext
from billing.app.ports import Clock, IdGenerator, PaymentGateway, UnitOfWork
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
        invoice_series=s.invoice_series,
        credit_note_series=s.credit_note_series,
        bill_of_supply_series=s.bill_of_supply_series,
        grace_days=s.grace_days,
        abandon_hours=s.checkout_abandon_hours,
        total_count_month=s.razorpay_total_count_month,
        total_count_year=s.razorpay_total_count_year,
    )


def make_context(
    config: BillingConfig,
    uow_factory: Callable[[], UnitOfWork],
    gateway: PaymentGateway,
    clock: Clock,
    ids: IdGenerator,
    invalidate: Callable[[UUID], Awaitable[None]],
) -> BillingContext:
    return BillingContext(uow_factory, gateway, clock, ids, config, invalidate)


def install_billing(app: FastAPI, bctx: BillingContext) -> EventApplier:
    """Attach what the REST routes and the idempotency middleware read from app.state."""
    applier = EventApplier(bctx)
    app.state.uow_factory = bctx.uow_factory
    app.state.gateway = bctx.gateway
    app.state.subscriptions = SubscriptionService(bctx, applier)
    return applier
