"""Pay as you Go top-ups: an order, a verified payment, and in one transaction the payment row, the
credit lot, the ledger posting and the tax invoice (Decision 6: GST is charged when the customer
pays, and the wallet holds the taxable value)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from billing.app.charges import _invoice_amounts
from billing.app.config import BillingContext
from billing.app.errors import InvalidSignature, NotFound, ValidationFailed
from billing.app.ports import Counters, GatewayOrder, GatewayPayment, UnitOfWork
from billing.app.records import CreditLot, InvoiceLineRecord, InvoiceRecord, PaymentRecord
from billing.domain.calendar import add_months, financial_year_code
from billing.domain.catalog import OwnerType
from billing.domain.india import is_state_code
from billing.domain.ledger import wallet_topup
from billing.domain.money import Paise, split_gst
from billing.observability import PAYMENTS

log = structlog.get_logger()
TOPUP_REF = "topup:"
RENDER_JOB = "invoice.render"


@dataclass(frozen=True, slots=True)
class TopupOptions:
    """What the frontend hands to Razorpay Checkout."""

    order_id: str
    amount_paise: int
    key: str
    name: str
    description: str
    email: str | None


def topup_ref(account_id: UUID) -> str:
    return f"{TOPUP_REF}{account_id}"


def account_of_order(order: GatewayOrder) -> UUID | None:
    ref = order.notes.get("billing_ref", "")
    if not ref.startswith(TOPUP_REF):
        return None
    try:
        return UUID(ref.removeprefix(TOPUP_REF))
    except ValueError:
        return None


async def apply_topup(
    uow: UnitOfWork, bctx: BillingContext, payment: GatewayPayment, order: GatewayOrder
) -> bool:
    """Record a captured top-up payment, in the caller's transaction. False when it was already
    recorded (a replayed webhook, or verify and the webhook both arriving)."""
    if payment.status != "captured" or payment.currency != "INR":
        raise ValueError(f"payment {payment.id} is {payment.status} {payment.currency}")
    account_id = account_of_order(order)
    if account_id is None:
        raise ValueError(f"order {order.id} is not a top-up")
    if payment.amount != order.amount:
        raise ValueError(f"payment {payment.id} is {payment.amount}, order is {order.amount}")
    account = await uow.accounts.get(account_id)
    if account is None or account.state_code is None:
        raise ValueError("a top-up needs a billing account with a GST state code")
    cfg = bctx.config
    payment_id = bctx.ids.new()
    inserted = await uow.payments.insert(
        PaymentRecord(
            id=payment_id,
            billing_account_id=account.id,
            purpose="topup",
            gateway_payment_id=payment.id,
            amount_paise=payment.amount,
            status="captured",
            captured_at=payment.created_at,
            raw=payment.raw,
            gateway_order_id=order.id,
            method=payment.method,
        )
    )
    if not inserted:
        return False
    PAYMENTS.labels(method=payment.method or "unknown", status="captured").inc()
    split = split_gst(
        payment.amount, cfg.supplier_state_code, account.state_code, registered=cfg.gst_registered
    )
    wallet = await uow.wallets.ensure(account.id)
    await uow.ledger.post(
        wallet_topup(split, account.id, f"topup:{payment.id}"),
        "payment",
        str(payment_id),
        payment.created_at,
    )
    await uow.wallets.add_lot(
        CreditLot(
            bctx.ids.new(),
            wallet.ledger_account_id,
            payment_id,
            split.taxable,
            split.taxable,
            add_months(payment.created_at, cfg.credit_validity_months),
        )
    )
    series = cfg.invoice_series if cfg.gst_registered else cfg.bill_of_supply_series
    number = await uow.invoices.allocate_number(series, financial_year_code(payment.created_at))
    invoice_id = bctx.ids.new()
    await uow.invoices.insert(
        InvoiceRecord(
            id=invoice_id,
            number=number,
            kind="tax_invoice" if cfg.gst_registered else "bill_of_supply",
            billing_account_id=account.id,
            issued_at=payment.created_at,
            customer_state_code=account.state_code,
            supplier_state_code=cfg.supplier_state_code,
            status="paid",
            payment_id=payment_id,
            customer_name=account.legal_name,
            customer_gstin=account.gstin,
            **_invoice_amounts(split),
        ),
        [
            InvoiceLineRecord(
                invoice_id=invoice_id,
                line_no=1,
                description=f"{cfg.brand_name} credits top-up",
                sac_code=cfg.sac_code,
                quantity=1,
                unit_price_paise=split.taxable,
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
    credited = int(split.taxable)
    owner_id, owner_type = account.owner_id, account.owner_type
    if bctx.credit_wallet is not None:
        credit = bctx.credit_wallet

        async def mirror() -> None:
            await credit(account.id, credited)

        uow.after_commit(mirror)
    if owner_type is OwnerType.USER:
        uow.after_commit(lambda: bctx.invalidate_entitlement(owner_id))
    return True


class WalletService:
    def __init__(self, bctx: BillingContext, counters: Counters | None = None) -> None:
        self._b = bctx
        self._counters = counters

    async def view(self, user_id: UUID) -> dict[str, Any] | None:
        """The wallet as the user sees it, or None before the first top-up."""
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            wallet = await uow.wallets.get(account.id) if account else None
            if account is None or wallet is None:
                return None
            balance = await uow.wallets.balance(account.id)
            lots = await uow.wallets.lots(wallet.ledger_account_id)
        mtd = 0
        if self._counters is not None:
            mtd = (await self._counters.wallet_get(account.id)).get("mtd", 0)
        return {
            "balance_paise": balance,
            "monthly_cap_paise": wallet.monthly_cap_paise,
            "month_to_date_paise": mtd,
            "lots": [
                {
                    "amount_paise": int(lot.amount_paise),
                    "remaining_paise": int(lot.remaining_paise),
                    "expires_at": lot.expires_at.isoformat(),
                }
                for lot in lots
            ],
        }

    async def transactions(self, user_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            if account is None or await uow.wallets.get(account.id) is None:
                return []
            return await uow.wallets.transactions(account.id, min(max(limit, 1), 200))

    async def set_cap(self, user_id: UUID, cap: Paise | None) -> None:
        """The monthly spend cap (taxable paise, like the balance); None removes it."""
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_or_create(OwnerType.USER, user_id)
            await uow.wallets.ensure(account.id)
            await uow.wallets.set_cap(account.id, cap)
        if self._counters is not None:
            await self._counters.wallet_set_cap(account.id, int(cap) if cap is not None else None)
        await self._b.invalidate_entitlement(user_id)  # a first wallet adds the PAYG grant

    async def start_topup(
        self,
        owner_type: OwnerType,
        owner_id: UUID,
        *,
        pack_code: str | None = None,
        amount: Paise | None = None,
        email: str | None,
        state_code: str | None,
    ) -> TopupOptions:
        """A user buys a pack from the catalog; an organisation (through staff) pays at least
        `org_min_topup`."""
        cfg = self._b.config
        if owner_type is OwnerType.USER:
            packs = dict(cfg.topup_packs)
            if pack_code not in packs:
                raise ValidationFailed(f"unknown top-up pack {pack_code!r}")
            gross = Paise(packs[pack_code])
        else:
            if amount is None or amount < cfg.org_min_topup:
                raise ValidationFailed(
                    f"an organisation top-up is at least {cfg.org_min_topup} paise"
                )
            gross = amount
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_or_create(owner_type, owner_id, email)
            code = state_code or account.state_code
            if not code or not is_state_code(code):
                raise ValidationFailed("a valid GST state code is required")
            if code != account.state_code:
                await uow.accounts.set_state_code(account.id, code)
        order = await self._b.gateway.create_order(  # outside any transaction
            gross,
            receipt=f"topup-{self._b.ids.new()}",
            notes={"billing_ref": topup_ref(account.id), "pack": pack_code or "custom"},
        )
        return TopupOptions(
            order.id,
            int(gross),
            cfg.razorpay_key_id,
            cfg.brand_name,
            f"{cfg.brand_name} credits",
            email or account.billing_email,
        )

    async def verify(self, user_id: UUID, order_id: str, payment_id: str, signature: str) -> int:
        """The frontend reports a finished Checkout. Returns the wallet balance (taxable paise).
        The webhook may get there first; either way the top-up is recorded once."""
        if not self._b.gateway.verify_order_signature(order_id, payment_id, signature):
            raise InvalidSignature()
        order = await self._b.gateway.fetch_order(order_id)
        payment = await self._b.gateway.fetch_payment(payment_id)
        account_id = account_of_order(order)
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            if account is None or account.id != account_id:
                raise NotFound("unknown order")
            if payment.order_id != order.id:
                raise ValidationFailed("the payment does not belong to this order")
            if payment.status == "captured":
                await apply_topup(uow, self._b, payment, order)
            return await uow.wallets.balance(account.id)

    async def apply_event(
        self, uow: UnitOfWork, payment: GatewayPayment, order: GatewayOrder
    ) -> bool:
        return await apply_topup(uow, self._b, payment, order)
