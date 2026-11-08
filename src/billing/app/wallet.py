"""Pay as you Go top-ups: an order, a verified payment, and in one transaction the payment row, the
credit lot, the ledger posting and the tax invoice (Decision 6: GST is charged when the customer
pays, and the wallet holds the taxable value)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from billing.app.charges import _invoice_amounts
from billing.app.config import BillingContext
from billing.app.errors import InvalidSignature, NotFound, ValidationFailed
from billing.app.ports import Counters, GatewayOrder, GatewayPayment, UnitOfWork
from billing.app.records import (
    BillingAccount,
    CreditLot,
    InvoiceLineRecord,
    InvoiceRecord,
    PaymentRecord,
)
from billing.domain.calendar import add_months, financial_year_code
from billing.domain.catalog import OwnerType
from billing.domain.india import is_state_code
from billing.domain.ledger import CLEARING, wallet_topup
from billing.domain.money import Paise, display_paise, split_gst, taxable_of
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
    cfg = bctx.config
    if not cfg.payg_enabled:
        return await _record_without_credit(uow, bctx, payment, order, account)
    if account is None or account.state_code is None:
        raise ValueError("a top-up needs a billing account with a GST state code")
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
            gateway=payment.gateway,
        )
    )
    if not inserted:
        return False
    PAYMENTS.labels(method=payment.method or "unknown", status="captured").inc()
    await credit_topup(
        uow, bctx, account, payment_id, payment.amount, payment.created_at,
        ledger_key=f"topup:{payment.id}",
    )  # fmt: skip
    return True


async def credit_topup(
    uow: UnitOfWork,
    bctx: BillingContext,
    account: BillingAccount,
    payment_id: UUID,
    gross: Paise,
    paid_at: datetime,
    *,
    ledger_key: str,
    received_in: str = CLEARING,
) -> None:
    """After the payment row exists, in the caller's transaction: the ledger posting (GST split by
    the account's state), the credit lot (valid `credit_validity_months`), the paid tax invoice
    (B2B when the account has a GSTIN) and, once committed, the Valkey mirror and the entitlement
    caches that depend on having a wallet."""
    cfg = bctx.config
    assert account.state_code is not None
    split = split_gst(
        gross, cfg.supplier_state_code, account.state_code, registered=cfg.gst_registered
    )
    wallet = await uow.wallets.ensure(account.id)
    await uow.ledger.post(
        wallet_topup(split, account.id, ledger_key, received_in=received_in),
        "payment",
        str(payment_id),
        paid_at,
    )
    await uow.wallets.add_lot(
        CreditLot(
            bctx.ids.new(),
            wallet.ledger_account_id,
            payment_id,
            split.taxable,
            split.taxable,
            add_months(paid_at, cfg.credit_validity_months),
        )
    )
    series = cfg.invoice_series if cfg.gst_registered else cfg.bill_of_supply_series
    number = await uow.invoices.allocate_number(series, financial_year_code(paid_at))
    invoice_id = bctx.ids.new()
    await uow.invoices.insert(
        InvoiceRecord(
            id=invoice_id,
            number=number,
            kind="tax_invoice" if cfg.gst_registered else "bill_of_supply",
            billing_account_id=account.id,
            issued_at=paid_at,
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
        RENDER_JOB, {"invoice_id": str(invoice_id)}, paid_at, f"{RENDER_JOB}:{invoice_id}"
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
    elif bctx.invalidate_org is not None:
        invalidate_org = bctx.invalidate_org

        async def clear_members() -> None:
            await invalidate_org(owner_id)

        uow.after_commit(clear_members)  # every member now overflows onto the org's credits


async def _record_without_credit(
    uow: UnitOfWork,
    bctx: BillingContext,
    payment: GatewayPayment,
    order: GatewayOrder,
    account: BillingAccount | None,
) -> bool:
    """PAYG_ENABLED is off but a top-up payment arrived (an order opened before the flag was
    turned off, or a forged one). The money has been taken, so the payment is recorded; nothing
    else happens: no wallet, no credit lot, no ledger posting, no invoice. The error log is the
    alert; the payment then needs a refund through the normal refund process."""
    if account is None:
        raise ValueError(f"top-up payment {payment.id} refers to an unknown billing account")
    inserted = await uow.payments.insert(
        PaymentRecord(
            id=bctx.ids.new(),
            billing_account_id=account.id,
            purpose="topup",
            gateway_payment_id=payment.id,
            amount_paise=payment.amount,
            status="captured",
            captured_at=payment.created_at,
            raw=payment.raw,
            gateway_order_id=order.id,
            method=payment.method,
            gateway=payment.gateway,
        )
    )
    if inserted:
        PAYMENTS.labels(method=payment.method or "unknown", status="captured").inc()
        log.error(
            "payg_topup_while_disabled",
            gateway_payment_id=payment.id,
            gateway_order_id=order.id,
            billing_account_id=str(account.id),
            amount_paise=int(payment.amount),
            action="payment recorded, no wallet credit; refund it",
        )
    return inserted


def amounts(registered: bool, name: str, taxable: int | None) -> dict[str, int | None]:
    """`name_paise` is what the user sees (GST included), `name_taxable_paise` what the wallet
    holds."""
    if taxable is None:
        return {f"{name}_paise": None, f"{name}_taxable_paise": None}
    return {
        f"{name}_paise": int(display_paise(Paise(taxable), registered)),
        f"{name}_taxable_paise": int(taxable),
    }


def wallet_block(
    registered: bool,
    balance: int,
    cap: int | None,
    month_to_date: int | None = None,
    lots: Sequence[CreditLot] = (),
) -> dict[str, Any]:
    block: dict[str, Any] = {
        **amounts(registered, "balance", balance),
        **amounts(registered, "monthly_cap", cap),
    }
    if month_to_date is not None:
        block.update(amounts(registered, "month_to_date", month_to_date))
        block["lots"] = [
            {
                **amounts(registered, "amount", int(lot.amount_paise)),
                **amounts(registered, "remaining", int(lot.remaining_paise)),
                "expires_at": lot.expires_at.isoformat(),
            }
            for lot in lots
        ]
    return block


def transaction_json(registered: bool, row: dict[str, Any]) -> dict[str, Any]:
    """Usage is shown at the unit price the user was quoted (Rs 4 for Best), a top-up at what was
    paid, anything else at the taxable value plus GST."""
    taxable = int(row["amount_paise"])
    shown = row.get("display_paise")
    if shown is None:
        sign = 1 if taxable >= 0 else -1
        shown = sign * int(display_paise(Paise(abs(taxable)), registered))
    out = {k: v for k, v in row.items() if k not in ("amount_paise", "display_paise")}
    return {**out, "amount_paise": int(shown), "amount_taxable_paise": taxable}


class WalletService:
    def __init__(self, bctx: BillingContext, counters: Counters | None = None) -> None:
        self._b = bctx
        self._counters = counters

    @property
    def enabled(self) -> bool:
        return self._b.config.payg_enabled

    @property
    def registered(self) -> bool:
        return self._b.config.gst_registered

    def stored_cap(self, cap: int | None) -> Paise | None:
        """A cap typed with GST included, as the wallet stores it (taxable)."""
        if cap is None:
            return None
        return taxable_of(Paise(cap)) if self.registered else Paise(cap)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise NotFound("Pay as you Go is not available")

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
        return wallet_block(
            self._b.config.gst_registered, balance, wallet.monthly_cap_paise, mtd, lots
        )

    async def transactions(self, user_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            if account is None or await uow.wallets.get(account.id) is None:
                return []
            rows = await uow.wallets.transactions(account.id, min(max(limit, 1), 200))
        return [transaction_json(self._b.config.gst_registered, r) for r in rows]

    async def set_cap(self, user_id: UUID, cap: Paise | None) -> None:
        """The monthly spend cap. `cap` is what the user typed (GST included); it is stored as
        taxable paise like the balance. None removes it."""
        self._require_enabled()
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_or_create(OwnerType.USER, user_id)
            await uow.wallets.ensure(account.id)
            stored = self.stored_cap(cap)
            await uow.wallets.set_cap(account.id, stored)
        if self._counters is not None:
            await self._counters.wallet_set_cap(
                account.id, int(stored) if stored is not None else None
            )
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
        self._require_enabled()
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
        self._require_enabled()
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
