from datetime import UTC, datetime, timedelta

import pytest

from billing.domain.catalog import Interval
from billing.domain.proration import period_interval

START = datetime(2026, 11, 1, 4, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    "days,expected",
    [
        (1, Interval.MONTH),
        (10, Interval.MONTH),
        (28, Interval.MONTH),
        (30, Interval.MONTH),
        (31, Interval.MONTH),  # the longest month
        (32, Interval.YEAR),
        (365, Interval.YEAR),
        (366, Interval.YEAR),
    ],
)
def test_the_length_of_a_period_says_month_or_year(days: int, expected: Interval) -> None:
    assert period_interval(START, START + timedelta(days=days)) is expected


def test_a_real_calendar_year_and_month() -> None:
    assert period_interval(START, datetime(2027, 11, 1, 4, 45, tzinfo=UTC)) is Interval.YEAR
    assert period_interval(START, datetime(2026, 12, 1, 4, 45, tzinfo=UTC)) is Interval.MONTH


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError):
        period_interval(datetime(2026, 1, 1), START)
