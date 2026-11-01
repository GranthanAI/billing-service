from datetime import datetime
from enum import StrEnum

from billing.domain.calendar import require_aware
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
