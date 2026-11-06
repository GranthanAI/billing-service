"""Settlements (bank, gateway fees) and monthly revenue recognition.

Both post to the ledger with fixed idempotency keys, so a rerun, or the manual fallback after the
daily sync, never posts a payment twice.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from uuid import UUID

import structlog

from billing.app.ports import Clock, PaymentGateway, SettlementItem, UnitOfWork
from billing.domain.calendar import IST, ist_day
from billing.domain.ledger import refund_settlement, revenue_recognition, settlement
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


# --- revenue recognition --------------------------------------------------------------------


def month_index(dt: datetime) -> int:
    local = dt.astimezone(IST)
    return local.year * 12 + local.month - 1


def slice_for(taxable: Paise, n: int) -> Paise:
    """The n-th of 12 monthly slices; the last one takes the rounding remainder."""
    base = int(taxable) // 12
    return Paise(int(taxable) - 11 * base if n == 12 else base)


class RevenueService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock) -> None:
        self._uow = uow_factory
        self._clock = clock

    async def recognise(self, now: datetime | None = None) -> int:
        """Run on the 1st of a month: move one twelfth of each live yearly subscription's current
        period from deferred to recognised revenue. The n-th run after the period began posts the
        n-th slice (n = 1..12), keyed by subscription and month, so a rerun posts nothing."""
        now = now or self._clock.now()
        run_month = month_index(now)
        key_month = f"{ist_day(now):%Y%m}"
        async with self._uow() as uow:
            subs = list(await uow.subscriptions.list_live_yearly())
        posted = 0
        for sub in subs:
            start = sub.state.current_period_start
            if start is None:
                continue
            n = run_month - month_index(start)
            if not 1 <= n <= 12:
                continue
            async with self._uow() as uow:
                payment = await uow.payments.latest_for_subscription(sub.id)
                invoice = await uow.invoices.by_payment(payment.id) if payment else None
                if invoice is None:
                    continue
                amount = slice_for(invoice.taxable_paise, n)
                if amount <= 0:
                    continue
                tx = revenue_recognition(amount, f"recognise:{sub.id}:{key_month}")
                if await uow.ledger.post(tx, "subscription", str(sub.id), now):
                    posted += 1
        return posted
