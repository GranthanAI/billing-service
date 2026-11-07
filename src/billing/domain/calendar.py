"""Business calendar: the Asia/Kolkata day and the Indian financial year."""

import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed; use a timezone-aware datetime")
    return dt


def ist_day(now: datetime) -> date:
    return require_aware(now).astimezone(IST).date()


def ist_day_key(now: datetime) -> str:
    return ist_day(now).strftime("%Y%m%d")


def ist_month_key(now: datetime) -> str:
    return ist_day(now).strftime("%Y%m")


def next_ist_midnight(now: datetime) -> datetime:
    tomorrow = ist_day(now) + timedelta(days=1)
    return datetime.combine(tomorrow, time(0, 0), tzinfo=IST).astimezone(UTC)


def add_months(dt: datetime, months: int) -> datetime:
    """The same wall-clock moment `months` later; the day is clamped (31 Jan + 1 month = 28 Feb)."""
    require_aware(dt)
    index = dt.month - 1 + months
    year, month = dt.year + index // 12, index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def financial_year_code(now: datetime) -> str:
    """April to March in IST: 2026-04-01 to 2027-03-31 is "2627"."""
    d = ist_day(now)
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"
