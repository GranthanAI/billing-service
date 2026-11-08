"""Organisation billing: yearly seats bought by invoice (Razorpay payment link or bank transfer).

An organisation subscription has `billing_mode = 'invoice'`: no mandate, no autopay. The flow is
  subscribe  -> an `incomplete` subscription and an UNPAID tax invoice to the organisation (B2B:
                its GSTIN and its state decide CGST+SGST or IGST)
  paid       -> by payment link (webhook `payment_link.paid`) or `mark-paid` (bank transfer); the
                payment, the ledger postings and `ChargeCaptured` all happen in one transaction
  renewal    -> `org.renewal` invoices the next year 30 days before the period ends; unpaid at the
                end of the period it becomes past due with ORG_GRACE_DAYS, then suspended
  seats      -> more seats: a prorated invoice, applied when it is paid; fewer: at renewal
All of it is idempotent: invoice numbers come from the locked counter, payments from unique ids.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from billing.app.config import BillingConfig, BillingContext
from billing.app.errors import (
    ChangePending,
    NotFound,
    SubscriptionStateError,
    ValidationFailed,
)
from billing.app.ports import GatewayPayment, UnitOfWork
from billing.app.records import (
    BillingAccount,
    InvoiceLineRecord,
    InvoiceRecord,
    OrgInfo,
    PaymentRecord,
    PlanVersionRecord,
    SubscriptionRecord,
)
from billing.app.transitions import IllegalTransition, apply_transition
from billing.domain.calendar import IST, add_months, financial_year_code
from billing.domain.catalog import Interval, OrgVariant, OwnerType
from billing.domain.india import is_state_code
from billing.domain.ledger import BANK, CLEARING, subscription_charge
from billing.domain.money import GstSplit, Paise, round_half_up, split_gst
from billing.domain.subscription import ChargeCaptured, ChargeFailed, SubscriptionState, SubStatus
from billing.observability import PAYMENTS

log = structlog.get_logger()

ORG_PLANS = ("org_pro", "org_max")
RENEWAL_NOTICE = timedelta(days=30)
RENDER_JOB = "invoice.render"
BATCH = 200


@dataclass(frozen=True, slots=True)
class IssuedInvoice:
    subscription_id: UUID
    invoice_id: UUID
    number: str
    total_paise: int
    payment_link_url: str | None = None


def _date(dt: datetime) -> str:
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b} {local.year}"


def plan_label(plan: PlanVersionRecord) -> str:
    base = plan.plan.plan_code.replace("_", " ").title()  # "Org Pro"
    return f"{base} ({plan.plan.variant.value.title()})" if plan.plan.variant else base


async def issue_invoice(
    uow: UnitOfWork,
    bctx: BillingContext,
    account: BillingAccount,
    *,
    subscription_id: UUID,
    gross: Paise,
    quantity: int,
    description: str,
    period_start: datetime,
    period_end: datetime,
    now: datetime,
    seat_change_to: int | None = None,
) -> InvoiceRecord:
    """An unpaid B2B invoice (or bill of supply when the supplier is not GST registered)."""
    cfg: BillingConfig = bctx.config
    if account.state_code is None:
        raise ValidationFailed("the organisation has no GST state code on record")
    split = split_gst(
        gross, cfg.supplier_state_code, account.state_code, registered=cfg.gst_registered
    )
    registered = cfg.gst_registered
    series = cfg.invoice_series if registered else cfg.bill_of_supply_series
    number = await uow.invoices.allocate_number(series, financial_year_code(now))
    invoice_id = bctx.ids.new()
    invoice = InvoiceRecord(
        id=invoice_id,
        number=number,
        kind="tax_invoice" if registered else "bill_of_supply",
        billing_account_id=account.id,
        issued_at=now,
        customer_state_code=account.state_code,
        supplier_state_code=cfg.supplier_state_code,
        taxable_paise=split.taxable,
        cgst_paise=split.cgst,
        sgst_paise=split.sgst,
        igst_paise=split.igst,
        total_paise=split.total,
        status="issued",  # unpaid
        period_start=period_start,
        period_end=period_end,
        customer_name=account.legal_name,
        customer_gstin=account.gstin,
        subscription_id=subscription_id,
        seat_change_to=seat_change_to,
    )
    await uow.invoices.insert(
        invoice,
        [
            InvoiceLineRecord(
                invoice_id=invoice_id,
                line_no=1,
                description=description,
                sac_code=cfg.sac_code,
                quantity=quantity,
                unit_price_paise=Paise(round_half_up(split.taxable, quantity)),
                taxable_paise=split.taxable,
            )
        ],
    )
    await uow.jobs.enqueue(
        RENDER_JOB, {"invoice_id": str(invoice_id)}, now, f"{RENDER_JOB}:{invoice_id}"
    )
    return invoice


async def settle_invoice(
    uow: UnitOfWork,
    bctx: BillingContext,
    invoice: InvoiceRecord,
    *,
    reference: str,
    paid_at: datetime,
    gateway_payment: GatewayPayment | None = None,
) -> bool:
    """Record that an invoice was paid, in the caller's transaction. Returns False (and does
    nothing) when it already was, so a replayed webhook or a second `mark-paid` is harmless."""
    if invoice.subscription_id is None:
        raise ValidationFailed("this invoice does not belong to a subscription")
    sub = await uow.subscriptions.lock(invoice.subscription_id)  # lock order: subscription first
    fresh = await uow.invoices.get(invoice.id)
    if fresh is None:
        raise NotFound("invoice not found")
    if fresh.status == "paid":
        return False
    if fresh.status != "issued":
        raise ValidationFailed(f"invoice {fresh.number} is {fresh.status}, not payable")
    if gateway_payment is not None and gateway_payment.amount != fresh.total_paise:
        raise ValueError(
            f"payment {gateway_payment.id} is {gateway_payment.amount} paise but invoice "
            f"{fresh.number} is {fresh.total_paise}"
        )
    true_up = fresh.seat_change_to is not None
    payment_ref = gateway_payment.id if gateway_payment else f"bank:{fresh.id}"
    payment_id = bctx.ids.new()
    inserted = await uow.payments.insert(
        PaymentRecord(
            id=payment_id,
            billing_account_id=fresh.billing_account_id,
            purpose="org_invoice" if true_up else "subscription",
            gateway_payment_id=payment_ref,
            amount_paise=fresh.total_paise,
            status="captured",
            captured_at=paid_at,
            raw=dict(gateway_payment.raw)
            if gateway_payment
            else {"method": "bank_transfer", "reference": reference},
            subscription_id=fresh.subscription_id,
            gateway_order_id=gateway_payment.order_id if gateway_payment else None,
            method=gateway_payment.method if gateway_payment else "bank_transfer",
            gateway=gateway_payment.gateway if gateway_payment else "razorpay",
        )
    )
    if not inserted:
        return False
    split = GstSplit(fresh.taxable_paise, fresh.cgst_paise, fresh.sgst_paise, fresh.igst_paise)
    # Both a full year and a seat true-up go to deferred revenue and are recognised month by
    # month (app/revenue.py); a true-up over the months left in the period.
    await uow.ledger.post(
        subscription_charge(
            split,
            Interval.YEAR,  # a true-up too: deferred, then recognised over what is left
            f"charge:{payment_ref}",
            received_in=CLEARING if gateway_payment else BANK,
        ),
        "payment",
        str(payment_id),
        paid_at,
    )
    await uow.invoices.mark_paid(fresh.id, payment_id)
    PAYMENTS.labels(
        method=(gateway_payment.method if gateway_payment else None) or "bank_transfer",
        status="captured",
    ).inc()
    if sub is None:
        return True
    if true_up:
        assert fresh.seat_change_to is not None
        await uow.subscriptions.save(replace(sub, seats=max(sub.seats, fresh.seat_change_to)))
        return True
    assert fresh.period_start is not None and fresh.period_end is not None
    try:
        sub = await apply_transition(
            uow,
            bctx,
            sub,
            ChargeCaptured(fresh.period_start, fresh.period_end, fresh.total_paise),
            cause_ref=f"invoice:{fresh.number}",
        )
    except IllegalTransition as exc:
        log.warning(
            "paid_invoice_not_applied", invoice=fresh.number, status=sub.state.status.value,
            trigger=exc.trigger_name,
        )  # fmt: skip
        return True  # the money is recorded; a person decides what to do about the subscription
    if sub.pending_seats is not None:  # a decrease that was waiting for this renewal
        await uow.subscriptions.save(replace(sub, seats=sub.pending_seats, pending_seats=None))
    return True


class OrgBilling:
    def __init__(self, bctx: BillingContext) -> None:
        self._b = bctx

    # --- subscribe ---

    async def subscribe(
        self,
        org: OrgInfo,
        plan_code: str,
        variant: OrgVariant,
        seats: int,
        *,
        start: datetime | None,
        with_link: bool,
        actor: UUID,
        reason: str,
    ) -> IssuedInvoice:
        if plan_code not in ORG_PLANS:
            raise ValidationFailed(f"{plan_code!r} is not an organisation plan")
        if org.type is not variant:
            raise ValidationFailed(f"the organisation is a {org.type.value}, not a {variant.value}")
        if not org.state_code or not is_state_code(org.state_code):
            raise ValidationFailed("org-service has no valid GST state code for the organisation")
        now = self._b.clock.now()
        begin = start or now
        async with self._b.uow_factory() as uow:
            plan = await uow.plans.current(plan_code, variant)
            if plan is None or plan.plan.price_year is None:
                raise ValidationFailed(f"no yearly price for {plan_code} ({variant.value})")
            minimum = plan.plan.min_seats or 1
            if seats < minimum:
                raise ValidationFailed(
                    f"{plan_code} for a {variant.value} needs at least {minimum} seats"
                )
            account = await uow.accounts.get_or_create(OwnerType.ORG, org.org_id, org.billing_email)
            await uow.accounts.set_state_code(account.id, org.state_code)
            await uow.accounts.set_gstin(account.id, org.gstin, org.name)
            account = await uow.accounts.get(account.id) or account
            if await uow.subscriptions.list_open_for_account(account.id):
                raise SubscriptionStateError("this organisation already has a subscription")
            sub = SubscriptionRecord(
                id=self._b.ids.new(),
                billing_account_id=account.id,
                state=SubscriptionState(SubStatus.INCOMPLETE, plan.plan.id, Interval.YEAR),
                seats=seats,
                billing_mode="invoice",
                gateway="invoice",
                created_at=now,
            )
            await uow.subscriptions.insert(sub)
            end = add_months(begin, 12)
            invoice = await issue_invoice(
                uow, self._b, account,
                subscription_id=sub.id,
                gross=Paise(seats * int(plan.plan.price_year)),
                quantity=seats,
                description=(
                    f"{self._b.config.brand_name} {plan_label(plan)} · yearly · {seats} seats · "
                    f"{_date(begin)} – {_date(end)}"
                ),
                period_start=begin,
                period_end=end,
                now=now,
            )  # fmt: skip
            await uow.audit.record(
                actor, "org.subscribe", "subscription", str(sub.id), reason, None,
                {"org_id": str(org.org_id), "plan": plan_code, "variant": variant.value,
                 "seats": seats, "invoice": invoice.number},
            )  # fmt: skip
        url = await self._link_best_effort(invoice.id, org) if with_link else None
        return IssuedInvoice(sub.id, invoice.id, invoice.number, int(invoice.total_paise), url)

    # --- payment ---

    async def mark_paid(
        self,
        invoice_id: UUID,
        *,
        reference: str,
        paid_at: datetime | None,
        amount: Paise | None,
        actor: UUID,
        reason: str,
    ) -> bool:
        """A bank transfer arrived. False when the invoice was already paid (nothing changed)."""
        async with self._b.uow_factory() as uow:
            invoice = await uow.invoices.get(invoice_id)
            if invoice is None or invoice.subscription_id is None:
                raise NotFound("invoice not found")
            if amount is not None and amount != invoice.total_paise:
                raise ValidationFailed(
                    f"the invoice is for {invoice.total_paise} paise, not {int(amount)}"
                )
            done = await settle_invoice(
                uow, self._b, invoice,
                reference=reference, paid_at=paid_at or self._b.clock.now(),
            )  # fmt: skip
            if done:
                await uow.audit.record(
                    actor, "invoice.mark_paid", "invoice", str(invoice_id), reason,
                    {"status": "issued"}, {"status": "paid", "reference": reference},
                )  # fmt: skip
        return done

    async def create_link(self, invoice_id: UUID, org: OrgInfo | None = None) -> str:
        """A Razorpay payment link for an unpaid invoice (made once; asking again returns it)."""
        async with self._b.uow_factory() as uow:
            invoice = await uow.invoices.get(invoice_id)
            if invoice is None:
                raise NotFound("invoice not found")
            if invoice.status != "issued":
                raise ValidationFailed(f"invoice {invoice.number} is {invoice.status}")
            if invoice.payment_link_url:
                return invoice.payment_link_url
            account = await uow.accounts.get(invoice.billing_account_id)
        assert account is not None
        link = await self._b.gateway.create_payment_link(  # outside any transaction
            invoice.total_paise,
            description=f"Invoice {invoice.number}",
            customer_name=(org.name if org else None) or account.legal_name or "Customer",
            customer_email=(org.billing_email if org else None) or account.billing_email,
            reference_id=str(invoice.id),
            notes={"invoice_id": str(invoice.id), "invoice_number": invoice.number},
        )
        async with self._b.uow_factory() as uow:
            await uow.invoices.set_payment_link(invoice.id, link.id, link.short_url)
        return link.short_url

    async def _link_best_effort(self, invoice_id: UUID, org: OrgInfo | None) -> str | None:
        try:
            return await self.create_link(invoice_id, org)
        except Exception:  # the invoice exists; the link can be made again from the admin API
            log.warning("payment_link_failed", invoice_id=str(invoice_id), exc_info=True)
            return None

    # --- seats ---

    async def change_seats(
        self, org_id: UUID, seats: int, *, actor: UUID, reason: str
    ) -> tuple[str, IssuedInvoice | None]:
        """More seats: ('invoiced', the prorated invoice). Fewer: ('at_renewal', None). Same
        count: ('unchanged', None) after clearing any waiting decrease."""
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.ORG, org_id)
            current = await uow.subscriptions.live_for_account(account.id) if account else None
            if account is None or current is None or current.billing_mode != "invoice":
                raise SubscriptionStateError("this organisation has no live subscription")
            sub = await uow.subscriptions.lock(current.id)
            assert sub is not None
            if sub.state.status not in (SubStatus.ACTIVE, SubStatus.PAST_DUE):
                raise SubscriptionStateError(f"the subscription is {sub.state.status.value}")
            plan = await uow.plans.by_id(sub.state.plan_version_id)
            assert plan is not None and plan.plan.price_year is not None
            minimum = plan.plan.min_seats or 1
            if seats < minimum:
                raise ValidationFailed(f"this plan needs at least {minimum} seats")
            if seats < sub.seats:
                await uow.subscriptions.save(replace(sub, pending_seats=seats))
                await uow.audit.record(
                    actor, "org.seats", "subscription", str(sub.id), reason,
                    {"seats": sub.seats}, {"pending_seats": seats},
                )  # fmt: skip
                return "at_renewal", None
            if seats == sub.seats:
                if sub.pending_seats is not None:
                    await uow.subscriptions.save(replace(sub, pending_seats=None))
                return "unchanged", None
            if any(i.seat_change_to for i in await uow.invoices.unpaid_for_subscription(sub.id)):
                raise ChangePending("pay or void the open seat invoice first")
            assert sub.state.current_period_end is not None
            days = (
                sub.state.current_period_end.astimezone(IST).date() - now.astimezone(IST).date()
            ).days
            days = max(0, min(days, 365))
            gross = Paise(
                round_half_up((seats - sub.seats) * int(plan.plan.price_year) * days, 365)
            )
            if gross <= 0:
                raise SubscriptionStateError("the period has ended; renew first")
            invoice = await issue_invoice(
                uow, self._b, account,
                subscription_id=sub.id,
                gross=gross,
                quantity=seats - sub.seats,
                description=(
                    f"{self._b.config.brand_name} {plan_label(plan)} · "
                    f"{seats - sub.seats} additional "
                    f"seats · {_date(now)} – {_date(sub.state.current_period_end)}"
                ),
                period_start=now,
                period_end=sub.state.current_period_end,
                now=now,
                seat_change_to=seats,
            )  # fmt: skip
            await uow.subscriptions.save(replace(sub, pending_seats=None))
            await uow.audit.record(
                actor, "org.seats", "subscription", str(sub.id), reason,
                {"seats": sub.seats}, {"seat_change_to": seats, "invoice": invoice.number},
            )  # fmt: skip
        url = await self._link_best_effort(invoice.id, None)
        return "invoiced", IssuedInvoice(
            sub.id, invoice.id, invoice.number, int(invoice.total_paise), url
        )

    # --- usage ---

    async def usage_report(
        self, members: list[UUID], start: date, end: date
    ) -> list[dict[str, Any]]:
        """Per member and model group: messages and billable units between two IST days."""
        rows: list[dict[str, Any]] = []
        async with self._b.uow_factory() as uow:
            for member in members:
                for r in await uow.admin.daily_usage(member, start, end):
                    rows.append({"user_id": str(member), **r})
        return rows

    # --- renewal ---

    async def renew(self) -> tuple[int, int]:
        """`org.renewal`: invoice the next year 30 days ahead; mark unpaid, ended periods past due.
        Returns (invoices issued, subscriptions marked past due)."""
        now = self._b.clock.now()
        async with self._b.uow_factory() as uow:
            due = list(await uow.subscriptions.list_invoice_mode_due(now + RENEWAL_NOTICE, BATCH))
        issued: list[UUID] = []
        overdue = 0
        for rec in due:
            try:
                async with self._b.uow_factory() as uow:
                    sub = await uow.subscriptions.lock(rec.id)
                    if sub is None or sub.state.current_period_end is None:
                        continue
                    end = sub.state.current_period_end
                    existing = await uow.invoices.for_period(sub.id, end)
                    if existing is None:
                        existing = await self._renewal_invoice(uow, sub, end, now)
                        if existing is not None:
                            issued.append(existing.id)
                    if sub.state.status is SubStatus.ACTIVE and end <= now:
                        try:
                            await apply_transition(
                                uow, self._b, sub, ChargeFailed(), cause_ref="org.renewal",
                                grace_days=self._b.config.org_grace_days,
                            )  # fmt: skip
                            overdue += 1
                        except IllegalTransition:
                            pass
            except Exception:
                log.exception("org_renewal_failed", subscription_id=str(rec.id))
        for invoice_id in issued:
            await self._link_best_effort(invoice_id, None)
        return len(issued), overdue

    async def _renewal_invoice(
        self, uow: UnitOfWork, sub: SubscriptionRecord, start: datetime, now: datetime
    ) -> InvoiceRecord | None:
        plan = await uow.plans.by_id(sub.state.plan_version_id)  # subscribers keep their version
        account = await uow.accounts.get(sub.billing_account_id)
        if plan is None or account is None or plan.plan.price_year is None:
            return None
        seats = sub.pending_seats or sub.seats
        end = add_months(start, 12)
        return await issue_invoice(
            uow, self._b, account,
            subscription_id=sub.id,
            gross=Paise(seats * int(plan.plan.price_year)),
            quantity=seats,
            description=(
                f"{self._b.config.brand_name} {plan_label(plan)} · yearly renewal · "
                f"{seats} seats · "
                f"{_date(start)} – {_date(end)}"
            ),
            period_start=start,
            period_end=end,
            now=now,
        )  # fmt: skip
