"""Settlements (bank, gateway fees).

Posts to the ledger with fixed idempotency keys, so a rerun, or the manual fallback after the daily
sync, never posts a payment twice.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from uuid import UUID

import structlog

from billing.app.ports import Clock, PaymentGateway, SettlementItem, UnitOfWork
from billing.domain.ledger import refund_settlement, settlement
from billing.domain.money import Paise

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SettlementReport:
    payments_posted: int = 0
    refunds_posted: int = 0
    already_posted: int = 0
    unsettled: int = 0
    unknown: int = 0  # not ours (or not recorded yet): left for the reconciliation to flag


def settle_key(payment_id: UUID) -> str:
    return f"settle:{payment_id}"


def refund_settle_key(refund_id: UUID) -> str:
    return f"settle-refund:{refund_id}"


def split_deductions(gross: Paise, credit: Paise, tax: Paise) -> tuple[Paise, Paise]:
    """(fee excluding GST, GST on the fee) for a payment whose bank credit is known.

    Whatever Razorpay held back from the payment is `gross - credit`; of that, `tax` is GST and
    the rest is the fee. This stays balanced whether Razorpay's own `fee` field includes the GST
    or not (to be confirmed against a real settlement; see the test-mode runbook)."""
    held = max(0, int(gross) - int(credit))
    gst = min(int(tax), held)
    return Paise(held - gst), Paise(gst)


class SettlementService:
    def __init__(
        self, uow_factory: Callable[[], UnitOfWork], gateway: PaymentGateway, clock: Clock
    ) -> None:
        self._uow = uow_factory
        self._gateway = gateway
        self._clock = clock

    async def sync(self, day: date) -> SettlementReport:
        items = await self._gateway.list_settlement_items(day)  # outside any transaction
        posted = refunds = already = unsettled = unknown = 0
        for item in items:
            if not item.settled:
                unsettled += 1
                continue
            outcome = await self._post_item(item)
            if outcome == "payment":
                posted += 1
            elif outcome == "refund":
                refunds += 1
            elif outcome == "already":
                already += 1
            else:
                unknown += 1
        report = SettlementReport(posted, refunds, already, unsettled, unknown)
        log.info("settlements_synced", day=day.isoformat(), **asdict(report))
        return report

    async def _post_item(self, item: SettlementItem) -> str:
        async with self._uow() as uow:
            when = item.settled_at or self._clock.now()
            if item.kind == "payment":
                payment = await uow.payments.by_gateway_id(item.entity_id)
                if payment is None:
                    return "unknown"
                fee, gst = split_deductions(payment.amount_paise, item.credit, item.tax)
                if payment.fee_paise is not None and payment.fee_gst_paise is not None:
                    stored = int(payment.fee_paise) + int(payment.fee_gst_paise)
                    if abs(stored - (int(fee) + int(gst))) > 1:
                        log.warning(
                            "settlement_fee_disagrees",
                            payment_id=str(payment.id),
                            gateway_payment_id=payment.gateway_payment_id,
                            bank_credit_deduction=int(fee) + int(gst),
                            stored_fee=int(payment.fee_paise),
                            stored_fee_gst=int(payment.fee_gst_paise),
                        )
                tx = settlement(payment.amount_paise, fee, gst, settle_key(payment.id))
                done = await uow.ledger.post(tx, "settlement", str(payment.id), when)
                return "payment" if done else "already"
            if item.kind == "refund":
                refund = await uow.refunds.by_gateway_id(item.entity_id)
                if refund is None:
                    return "unknown"
                tx = refund_settlement(refund.amount_paise, refund_settle_key(refund.id))
                done = await uow.ledger.post(tx, "refund_settlement", str(refund.id), when)
                return "refund" if done else "already"
            return "unknown"  # transfers and adjustments need a person

    async def post_manual(self, payment_id: UUID, fee: Paise, fee_gst: Paise) -> bool:
        """The fallback: an admin enters a payment's fee and GST by hand. False if that payment's
        settlement is already in the ledger."""
        async with self._uow() as uow:
            payment = await uow.payments.get(payment_id)
            if payment is None:
                raise LookupError(f"payment {payment_id} not found")
            tx = settlement(payment.amount_paise, fee, fee_gst, settle_key(payment.id))
            return await uow.ledger.post(tx, "settlement", str(payment.id), self._clock.now())
