from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from billing.domain.calendar import add_months, require_aware
from billing.domain.catalog import Interval
from billing.domain.money import Paise, round_half_up


class ChangeKind(StrEnum):
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"


class NoChange(Exception):
    pass


def unused_credit(
    paid: Paise, period_start: datetime, period_end: datetime, now: datetime
) -> Paise:
    """Share of `paid` for [start, end) that is still unused at `now`."""
    require_aware(period_start)
    require_aware(period_end)
    require_aware(now)
    if paid < 0:
        raise ValueError("paid must be >= 0")
    total = int((period_end - period_start).total_seconds())
    if total <= 0:
        raise ValueError("period_end must be after period_start")
    remaining = min(max(int((period_end - now).total_seconds()), 0), total)
    return Paise(round_half_up(paid * remaining, total))


def period_interval(period_start: datetime, period_end: datetime) -> Interval:
    """Month or year, from the length of a billing period (more than 31 days is a year). This is
    what a payment's invoice says it paid for, whatever the subscription's interval is now."""
    require_aware(period_start)
    require_aware(period_end)
    return Interval.YEAR if period_end - period_start > timedelta(days=31) else Interval.MONTH


def classify_change(
    old_rank: int, old_interval: Interval, new_rank: int, new_interval: Interval
) -> ChangeKind:
    if old_rank == new_rank and old_interval == new_interval:
        raise NoChange
    if new_rank > old_rank:
        return ChangeKind.UPGRADE
    if new_rank == old_rank and old_interval is Interval.MONTH and new_interval is Interval.YEAR:
        return ChangeKind.UPGRADE
    return ChangeKind.DOWNGRADE


def upgrade_refund(
    last_period_paid: Paise | None,
    period_start: datetime | None,
    period_end: datetime | None,
    now: datetime,
) -> Paise:
    """What goes back to the original payment method when an upgrade is charged: the unused share of the period paid.
    The subscription state machine and the preview both call this, so they cannot disagree."""
    if last_period_paid is None or period_start is None or period_end is None:
        return Paise(0)
    return unused_credit(last_period_paid, period_start, period_end, now)


@dataclass(frozen=True, slots=True)
class ChangeQuote:
    """What a plan change or resume costs and when it takes effect. Amounts are paise, GST included."""

    kind: str  # upgrade | downgrade | interval_change | resume
    amount_due_now_paise: int
    refund_paise: int
    effective_at: datetime
    new_period_end: datetime
    price_paise: int  # the new plan's price for the new interval

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "amount_due_now_paise": self.amount_due_now_paise,
            "refund_paise": self.refund_paise,
            "effective_at": self.effective_at.isoformat(),
            "new_period_end": self.new_period_end.isoformat(),
            "price_paise": self.price_paise,
        }


def _months(interval: Interval) -> int:
    return 12 if interval is Interval.YEAR else 1


def quote_change(
    *,
    direction: ChangeKind,
    same_plan: bool,
    last_period_paid: Paise | None,
    period_start: datetime | None,
    period_end: datetime | None,
    now: datetime,
    new_price: Paise,
    new_interval: Interval,
) -> ChangeQuote:
    """An upgrade is charged now (a new period starts now) and the unused days of the old period go back. A downgrade
    starts when the paid period ends and costs nothing now."""
    label = "interval_change" if same_plan else direction.value
    if direction is ChangeKind.UPGRADE:
        return ChangeQuote(
            label,
            int(new_price),
            int(upgrade_refund(last_period_paid, period_start, period_end, now)),
            now,
            add_months(now, _months(new_interval)),
            int(new_price),
        )
    start = period_end or now
    return ChangeQuote(label, 0, 0, start, add_months(start, _months(new_interval)), int(new_price))


def quote_resume(*, period_end: datetime, price: Paise, interval: Interval) -> ChangeQuote:
    """Resuming a cancelling subscription: authorise now, charged when the paid period ends."""
    return ChangeQuote(
        "resume", 0, 0, period_end, add_months(period_end, _months(interval)), int(price)
    )
