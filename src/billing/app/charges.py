"""Payments, GST invoices, credit notes and the ledger postings behind them."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from billing.app.config import BillingConfig, BillingContext
from billing.app.ports import GatewayPayment, IdGenerator, UnitOfWork
from billing.app.records import (
    InvoiceLineRecord,
    InvoiceRecord,
    PaymentRecord,
    RefundRecord,
    SubscriptionRecord,
)
from billing.app.revenue import release_deferred
from billing.domain.calendar import IST, financial_year_code
from billing.domain.catalog import Interval
from billing.domain.ledger import subscription_charge, subscription_refund
from billing.domain.money import GstSplit, Paise, round_half_up, split_gst
from billing.domain.proration import period_interval
from billing.observability import PAYMENTS

log = structlog.get_logger()
RENDER_JOB = "invoice.render"


def _date(dt: datetime) -> str:
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b} {local.year}"


def charge_description(
    brand: str, plan_code: str, interval: Interval, start: datetime, end: datetime
) -> str:
    word = "monthly" if interval is Interval.MONTH else "yearly"
    return f"{brand} {plan_code.title()} · {word} · {_date(start)} – {_date(end)}"


def _invoice_amounts(split: GstSplit) -> dict[str, Any]:
    return {
        "taxable_paise": split.taxable,
        "cgst_paise": split.cgst,
        "sgst_paise": split.sgst,
        "igst_paise": split.igst,
        "total_paise": split.total,
    }


def fee_without_gst(fee: Paise | None, tax: Paise | None) -> Paise | None:
    """Razorpay's payment `fee` is "Fee (including GST) charged by Razorpay" and `tax` is the GST in
    it, so the fee proper is fee - tax. Stored that way, `fee_paise + fee_gst_paise` is what
    Razorpay deducts, and the settlement posting (fee expense + GST input) uses the same split."""
    if fee is None:
        return None
    return Paise(max(0, int(fee) - int(tax or 0)))


async def record_charge(
    uow: UnitOfWork,
    subscription: SubscriptionRecord,
    payment: GatewayPayment,
    period_start: datetime,
    period_end: datetime,
    interval: Interval,
    seats: int = 1,
    *,
    plan_code: str,
    config: BillingConfig,
    ids: IdGenerator,
    purpose: str = "subscription",
) -> bool:
    """Payment row, GST invoice and ledger postings, in the caller's transaction.

    Returns False, having written nothing more, when the payment was already recorded.
    """
    if payment.status != "captured":
        raise ValueError(f"payment {payment.id} is {payment.status}, not captured")
    if payment.currency != "INR":
        raise ValueError(f"payment {payment.id} is in {payment.currency}")
    payment_id = ids.new()
    inserted = await uow.payments.insert(
        PaymentRecord(
            id=payment_id,
            billing_account_id=subscription.billing_account_id,
            purpose=purpose,
            gateway_payment_id=payment.id,
            amount_paise=payment.amount,
            status="captured",
            captured_at=payment.created_at,
            raw=payment.raw,
            subscription_id=subscription.id,
            gateway_order_id=payment.order_id,
            method=payment.method,
            fee_paise=fee_without_gst(payment.fee, payment.tax),
            fee_gst_paise=payment.tax,
        )
    )
    if not inserted:
        return False
    PAYMENTS.labels(method=payment.method or "unknown", status="captured").inc()

    account = await uow.accounts.get(subscription.billing_account_id)
    if account is None or account.state_code is None:
        raise ValueError("billing account has no GST state code; checkout requires one")
    split = split_gst(
        payment.amount,
        config.supplier_state_code,
        account.state_code,
        registered=config.gst_registered,
    )
    # Lock order: ledger_balances before invoice_counters.
    await uow.ledger.post(
        subscription_charge(split, interval, f"charge:{payment.id}"),
        "payment",
        str(payment_id),
        payment.created_at,
    )
    registered = config.gst_registered
    series = config.invoice_series if registered else config.bill_of_supply_series
    number = await uow.invoices.allocate_number(series, financial_year_code(payment.created_at))
    invoice_id = ids.new()
    await uow.invoices.insert(
        InvoiceRecord(
            id=invoice_id,
            number=number,
            kind="tax_invoice" if registered else "bill_of_supply",
            billing_account_id=account.id,
            issued_at=payment.created_at,
            customer_state_code=account.state_code,
            supplier_state_code=config.supplier_state_code,
            status="paid",
            payment_id=payment_id,
            period_start=period_start,
            period_end=period_end,
            customer_name=account.legal_name,
            customer_gstin=account.gstin,
            **_invoice_amounts(split),
        ),
        [
            InvoiceLineRecord(
                invoice_id=invoice_id,
                line_no=1,
                description=charge_description(
                    config.brand_name, plan_code, interval, period_start, period_end
                ),
                sac_code=config.sac_code,
                quantity=seats,
                unit_price_paise=Paise(round_half_up(split.taxable, seats)),
                taxable_paise=split.taxable,
            )
        ],
    )
    await uow.jobs.enqueue(
        RENDER_JOB,
        {"invoice_id": str(invoice_id)},
        payment.created_at,
        f"{RENDER_JOB}:{invoice_id}",
    )
    return True


async def issue_refund(bctx: BillingContext, payload: Mapping[str, Any]) -> None:
    """The `refund.issue` job. Safe to run twice: Razorpay is asked through `refund_key`, and the
    rows are written only if the gateway refund is new to us."""
    payment_uuid = UUID(str(payload["payment_id"]))
    requested = Paise(int(payload["amount"]))
    reason = str(payload["reason"])
    refund_key = str(payload["refund_key"])
    interval = Interval(str(payload.get("interval", "month")))
    cfg = bctx.config

    async with bctx.uow_factory() as uow:
        payment = await uow.payments.get(payment_uuid)
        if payment is None:
            raise LookupError(f"payment {payment_uuid} not found")
        already = await uow.refunds.total_for_payment(payment_uuid)
    amount = Paise(min(requested, payment.amount_paise - already))
    if amount <= 0:
        log.warning("refund_nothing_left", payment_id=str(payment_uuid), refund_key=refund_key)
        return

    # outside any transaction
    refund = await bctx.gateway.refund_payment(
        payment.gateway_payment_id, amount, notes={"refund_key": refund_key, "reason": reason}
    )

    now = bctx.clock.now()
    async with bctx.uow_factory() as uow:
        refund_id = bctx.ids.new()
        if not await uow.refunds.insert(
            RefundRecord(refund_id, payment_uuid, refund.id, refund.amount, reason, refund.status)
        ):
            return
        await uow.payments.mark_refunded(
            payment_uuid, await uow.refunds.total_for_payment(payment_uuid)
        )
        original = await uow.invoices.by_payment(payment_uuid)
        if original is None:
            raise LookupError(f"payment {payment_uuid} has no invoice to credit")
        if original.period_start is not None and original.period_end is not None:
            # what the payment paid for, not the subscription's interval now (it may have changed)
            interval = period_interval(original.period_start, original.period_end)
        split = split_gst(
            refund.amount,
            original.supplier_state_code,
            original.customer_state_code,
            registered=original.kind == "tax_invoice",
        )
        await uow.ledger.post(
            subscription_refund(split, interval, f"refund:{refund_key}"),
            "refund",
            str(refund_id),
            now,
        )
        number = await uow.invoices.allocate_number(
            cfg.credit_note_series, financial_year_code(now)
        )
        note_id = bctx.ids.new()
        await uow.invoices.insert(
            InvoiceRecord(
                id=note_id,
                number=number,
                kind="credit_note",
                billing_account_id=original.billing_account_id,
                issued_at=now,
                customer_state_code=original.customer_state_code,
                supplier_state_code=original.supplier_state_code,
                status="issued",
                original_invoice_id=original.id,
                payment_id=payment_uuid,
                period_start=original.period_start,
                period_end=original.period_end,
                customer_name=original.customer_name,
                customer_gstin=original.customer_gstin,
                **_invoice_amounts(split),
            ),
            [
                InvoiceLineRecord(
                    invoice_id=note_id,
                    line_no=1,
                    description=f"Credit note against {original.number} · {reason}",
                    sac_code=cfg.sac_code,
                    quantity=1,
                    unit_price_paise=split.taxable,
                    taxable_paise=split.taxable,
                )
            ],
        )
        await uow.jobs.enqueue(
            RENDER_JOB, {"invoice_id": str(note_id)}, now, f"{RENDER_JOB}:{note_id}"
        )
        if (
            reason == "upgrade"
        ):  # the old period is superseded: what the refund did not return is earned
            await release_deferred(uow, payment, now)
