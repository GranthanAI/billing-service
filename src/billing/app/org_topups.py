"""Organisation wallet top-ups, started by staff: a pending request with a Razorpay payment link,
settled by the `payment_link.paid` webhook or by `mark-paid` (a bank transfer).

Settling is one transaction under a row lock on the request: the payment row, the credit lot, the
ledger posting (GST split with the organisation's state), a paid B2B tax invoice and the request
turning `paid`. The Valkey balance is raised after the commit. Whoever gets the lock first wins;
the other finds the request paid and changes nothing.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from billing.app.config import BillingContext
from billing.app.errors import NotFound, SubscriptionStateError, ValidationFailed
from billing.app.ports import GatewayPayment, UnitOfWork
from billing.app.records import OrgInfo, PaymentRecord, TopupRequest
from billing.app.wallet import credit_topup
from billing.domain.catalog import OwnerType
from billing.domain.india import is_state_code
from billing.domain.ledger import BANK, CLEARING
from billing.domain.money import Paise, split_gst
from billing.observability import PAYMENTS

log = structlog.get_logger()
PAISE_PER_RUPEE = 100


async def apply_org_topup(
    uow: UnitOfWork,
    bctx: BillingContext,
    request: TopupRequest,
    *,
    payment: GatewayPayment | None,
    reference: str,
    paid_at: datetime,
) -> bool:
    """Settle a top-up request, in the caller's transaction. `request` must have been read with
    `lock=True`. False when it was already paid (nothing changes). `payment` is None for a bank
    transfer."""
    fresh = await uow.topups.get(request.id, lock=True)
    if fresh is None:
        raise NotFound("top-up request not found")
    if fresh.status == "paid":
        return False
    if payment is not None and payment.amount != fresh.amount_paise:
        raise ValueError(
            f"payment {payment.id} is {payment.amount} paise but top-up {fresh.id} is "
            f"{fresh.amount_paise}"
        )
    account = await uow.accounts.get(fresh.billing_account_id)
    if account is None:
        raise NotFound("billing account not found")
    payment_ref = payment.id if payment else f"bank:{reference}"
    payment_id = bctx.ids.new()
    inserted = await uow.payments.insert(
        PaymentRecord(
            id=payment_id,
            billing_account_id=account.id,
            purpose="topup",
            gateway_payment_id=payment_ref,
            amount_paise=fresh.amount_paise,
            status="captured",
            captured_at=paid_at,
            raw=dict(payment.raw)
            if payment
            else {"method": "bank_transfer", "reference": reference},
            gateway_order_id=payment.order_id if payment else None,
            method=payment.method if payment else "bank_transfer",
        )
    )
    if not inserted:
        return False  # this payment id was recorded already
    PAYMENTS.labels(
        method=(payment.method if payment else "bank_transfer") or "unknown", status="captured"
    ).inc()
    if not bctx.config.payg_enabled:
        log.error(
            "payg_topup_while_disabled",
            gateway_payment_id=payment_ref,
            billing_account_id=str(account.id),
            amount_paise=int(fresh.amount_paise),
            action="payment recorded, no wallet credit; refund it",
        )
        return True
    if account.state_code is None:
        raise ValueError("an organisation top-up needs the organisation's GST state code")
    await credit_topup(
        uow, bctx, account, payment_id, fresh.amount_paise, paid_at,
        ledger_key=f"topup:{payment_ref}",
        received_in=CLEARING if payment else BANK,
    )  # fmt: skip
    await uow.topups.mark_paid(fresh.id, payment_id)
    return True


class OrgTopups:
    def __init__(self, bctx: BillingContext) -> None:
        self._b = bctx

    @property
    def enabled(self) -> bool:
        return self._b.config.payg_enabled

    def require_enabled(self) -> None:
        if not self.enabled:
            raise NotFound("Pay as you Go is not available")

    async def _request_of(self, uow: UnitOfWork, org_id: UUID, request_id: UUID) -> TopupRequest:
        account = await uow.accounts.get_by_owner(OwnerType.ORG, org_id)
        request = await uow.topups.get(request_id, lock=True)
        if account is None or request is None or request.billing_account_id != account.id:
            raise NotFound("top-up request not found")
        return request

    async def create(
        self, org: OrgInfo, amount: Paise, *, actor: UUID, reason: str
    ) -> dict[str, Any]:
        self.require_enabled()
        cfg = self._b.config
        if amount < cfg.org_min_topup:
            raise ValidationFailed(
                f"an organisation top-up is at least {cfg.org_min_topup} paise "
                f"(Rs {cfg.org_min_topup // PAISE_PER_RUPEE})"
            )
        if amount % PAISE_PER_RUPEE:
            raise ValidationFailed("an organisation top-up is a whole number of rupees")
        if not org.state_code or not is_state_code(org.state_code):
            raise ValidationFailed("org-service has no valid GST state code for the organisation")
        split = split_gst(
            amount, cfg.supplier_state_code, org.state_code, registered=cfg.gst_registered
        )
        request_id = self._b.ids.new()
        async with self._b.uow_factory() as uow:
            account = await uow.accounts.get_or_create(OwnerType.ORG, org.org_id, org.billing_email)
            await uow.accounts.set_state_code(account.id, org.state_code)
            await uow.accounts.set_gstin(account.id, org.gstin, org.name)
            now = self._b.clock.now()
            await uow.topups.insert(
                TopupRequest(
                    request_id, account.id, amount, "pending", None, None, actor, reason, now, now
                )
            )
            await uow.audit.record(
                actor, "org.topup_create", "topup_request", str(request_id), reason, None,
                {"org_id": str(org.org_id), "amount_paise": int(amount)},
            )  # fmt: skip
        try:
            link = await self._b.gateway.create_payment_link(  # outside any transaction
                amount,
                description=f"{cfg.brand_name} credits top-up",
                customer_name=org.name,
                customer_email=org.billing_email,
                reference_id=str(request_id),
                notes={"kind": "org_topup", "request_id": str(request_id)},
            )
        except Exception:
            async with self._b.uow_factory() as uow:
                await uow.topups.cancel(request_id)  # no link, nothing to pay
            raise
        async with self._b.uow_factory() as uow:
            await uow.topups.set_link(request_id, link.id)
        return {
            "request_id": request_id,
            "status": "pending",
            "amount_paise": int(amount),
            "credit_taxable_paise": int(split.taxable),
            "payment_link_url": link.short_url,
        }

    async def mark_paid(
        self, org_id: UUID, request_id: UUID, *, reference: str, actor: UUID, reason: str
    ) -> dict[str, Any]:
        """A bank transfer arrived. Idempotent: a request that is already paid changes nothing."""
        self.require_enabled()
        link_to_close: str | None = None
        async with self._b.uow_factory() as uow:
            request = await self._request_of(uow, org_id, request_id)
            if request.status == "cancelled":
                raise SubscriptionStateError("this top-up request was cancelled")
            changed = await apply_org_topup(
                uow, self._b, request,
                payment=None, reference=reference, paid_at=self._b.clock.now(),
            )  # fmt: skip
            if changed:
                link_to_close = request.gateway_link_id
                await uow.audit.record(
                    actor, "org.topup_mark_paid", "topup_request", str(request_id), reason,
                    {"status": "pending"}, {"status": "paid", "reference": reference},
                )  # fmt: skip
            current = await uow.topups.get(request_id)
        assert current is not None
        if link_to_close:  # the customer must not pay the same top-up twice
            try:
                await self._b.gateway.cancel_payment_link(link_to_close)
            except Exception:
                log.warning("payment_link_cancel_failed", link=link_to_close, exc_info=True)
        return {"request_id": request_id, "status": current.status, "changed": changed}

    async def cancel(
        self, org_id: UUID, request_id: UUID, *, actor: UUID, reason: str
    ) -> dict[str, Any]:
        """Only while pending. The payment link is cancelled first (outside any transaction), so a
        payment cannot arrive after the request is closed."""
        self.require_enabled()
        async with self._b.uow_factory() as uow:
            request = await self._request_of(uow, org_id, request_id)
            if request.status == "paid":
                raise SubscriptionStateError("this top-up was already paid")
            if request.status == "cancelled":
                return {"request_id": request_id, "status": "cancelled", "changed": False}
            link_id = request.gateway_link_id
        if link_id:
            await self._b.gateway.cancel_payment_link(link_id)
        async with self._b.uow_factory() as uow:
            request = await self._request_of(uow, org_id, request_id)
            if request.status == "paid":  # paid while the link was being cancelled
                raise SubscriptionStateError("this top-up was already paid")
            if request.status == "pending":
                await uow.topups.cancel(request_id)
                await uow.audit.record(
                    actor, "org.topup_cancel", "topup_request", str(request_id), reason,
                    {"status": "pending"}, {"status": "cancelled"},
                )  # fmt: skip
        return {"request_id": request_id, "status": "cancelled", "changed": True}
