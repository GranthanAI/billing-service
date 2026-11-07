"""Revenue recognition for yearly plans.

A yearly charge books its taxable value to deferred revenue. Each month a twelfth moves to
recognised revenue (`RevenueService.recognise`), and when the money stops being owed for the rest of
the period (an upgrade that refunds the unused part, a subscription that ends early or an account
that is deleted) `release_deferred` moves whatever is left, so deferred revenue for a payment always
returns to zero. Every posting has a fixed idempotency key, so reruns post nothing.
"""

from collections.abc import Callable
from datetime import datetime

from billing.app.ports import Clock, UnitOfWork
from billing.app.records import PaymentRecord
from billing.domain.calendar import IST
from billing.domain.catalog import Interval
from billing.domain.ledger import revenue_recognition
from billing.domain.money import Paise
from billing.domain.proration import period_interval


def month_index(dt: datetime) -> int:
    local = dt.astimezone(IST)
    return local.year * 12 + local.month - 1


def slice_for(taxable: Paise, n: int) -> Paise:
    """The n-th of 12 monthly slices; the last one takes the rounding remainder."""
    base = int(taxable) // 12
    return Paise(int(taxable) - 11 * base if n == 12 else base)


def slice_key(subscription_id: object, payment_id: object, n: int) -> str:
    return f"recognise:{subscription_id}:{payment_id}:{n}"


def release_key(payment_id: object) -> str:
    return f"release:{payment_id}"


async def deferred_remaining(uow: UnitOfWork, payment: PaymentRecord) -> Paise:
    """What of a yearly payment is still sitting in deferred revenue:
    taxable value - recognised - taxable value refunded. Zero for anything that is not yearly."""
    invoice = await uow.invoices.by_payment(payment.id)
    if invoice is None or invoice.period_start is None or invoice.period_end is None:
        return Paise(0)
    if invoice.seat_change_to is not None:
        return Paise(0)  # a seat true-up is booked to revenue when it is paid
    if period_interval(invoice.period_start, invoice.period_end) is not Interval.YEAR:
        return Paise(0)  # monthly charges go straight to revenue
    recognised = await uow.ledger.recognised_for_payment(payment.id)
    refunded = await uow.invoices.credited_taxable(payment.id)
    return Paise(max(0, int(invoice.taxable_paise) - recognised - int(refunded)))


async def release_deferred(uow: UnitOfWork, payment: PaymentRecord, now: datetime) -> Paise:
    """Post a final revenue recognition for what is left of a yearly payment. Returns the amount
    (0 when there was nothing to release). Safe to call twice."""
    remaining = await deferred_remaining(uow, payment)
    if remaining <= 0:
        return Paise(0)
    tx = revenue_recognition(remaining, release_key(payment.id))
    if not await uow.ledger.post(tx, "payment", str(payment.id), now):
        return Paise(0)
    return remaining


class RevenueService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock) -> None:
        self._uow = uow_factory
        self._clock = clock

    async def recognise(self, now: datetime | None = None) -> int:
        """Run on the 1st of a month. For each live yearly subscription the n-th run after the
        period began (n = 1..12) makes sure slices 1..n are all posted, so a month that was skipped
        is caught up by the next run. Each slice is capped at what is still deferred, so a partial
        refund never drives deferred revenue negative. Returns how many subscriptions posted."""
        now = now or self._clock.now()
        run_month = month_index(now)
        async with self._uow() as uow:
            subs = list(await uow.subscriptions.list_live_yearly())
        touched = 0
        for sub in subs:
            start = sub.state.current_period_start
            if start is None:
                continue
            n = min(run_month - month_index(start), 12)
            if n < 1:
                continue
            async with self._uow() as uow:
                payment = await uow.payments.latest_for_subscription(sub.id)
                invoice = await uow.invoices.by_payment(payment.id) if payment else None
                if payment is None or invoice is None:
                    continue
                posted = False
                for k in range(1, n + 1):
                    room = await deferred_remaining(uow, payment)
                    amount = Paise(min(int(slice_for(invoice.taxable_paise, k)), int(room)))
                    if amount <= 0:
                        continue
                    tx = revenue_recognition(amount, slice_key(sub.id, payment.id, k))
                    posted |= await uow.ledger.post(tx, "subscription", str(sub.id), now)
                touched += int(posted)
        return touched
