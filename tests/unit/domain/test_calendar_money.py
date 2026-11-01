from datetime import UTC, datetime

import pytest

from billing.domain.calendar import (
    financial_year_code,
    ist_day,
    ist_day_key,
    ist_month_key,
    next_ist_midnight,
)
from billing.domain.money import Paise, round_half_up, split_gst, taxable_of


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_ist_day_boundary() -> None:
    assert str(ist_day(utc(2026, 11, 1, 18, 29, 59))) == "2026-11-01"
    assert str(ist_day(utc(2026, 11, 1, 18, 30, 0))) == "2026-11-02"
    assert ist_day_key(utc(2026, 11, 1, 5)) == "20261101"
    assert ist_month_key(utc(2026, 11, 1, 5)) == "202611"


def test_next_midnight() -> None:
    assert next_ist_midnight(utc(2026, 11, 1, 5)) == utc(2026, 11, 1, 18, 30)
    assert next_ist_midnight(utc(2026, 11, 1, 18, 30)) == utc(2026, 11, 2, 18, 30)


def test_financial_year() -> None:
    assert financial_year_code(utc(2027, 3, 31, 18, 29, 59)) == "2627"
    assert financial_year_code(utc(2027, 3, 31, 18, 30, 0)) == "2728"
    assert financial_year_code(utc(2026, 4, 1, 0)) == "2627"
    assert financial_year_code(utc(2026, 3, 31, 18, 30)) == "2627"
    assert financial_year_code(utc(2026, 3, 31, 18, 29)) == "2526"


def test_naive_raises() -> None:
    with pytest.raises(ValueError):
        ist_day(datetime(2026, 11, 1))
    with pytest.raises(ValueError):
        financial_year_code(datetime(2026, 11, 1))


def test_round_half_up() -> None:
    assert round_half_up(5, 2) == 3
    assert round_half_up(7, 2) == 4
    assert round_half_up(4, 3) == 1
    with pytest.raises(ValueError):
        round_half_up(-1, 2)
    with pytest.raises(ValueError):
        round_half_up(1, 0)


@pytest.mark.parametrize(
    ("gross", "cust", "taxable", "cgst", "sgst", "igst"),
    [
        (49900, "10", 42288, 3806, 3806, 0),
        (49900, "29", 42288, 0, 0, 7612),
        (149900, "10", 127034, 11433, 11433, 0),
        (19900, "10", 16864, 1518, 1518, 0),
        (100, "10", 85, 7, 8, 0),
    ],
)
def test_split_gst(gross: int, cust: str, taxable: int, cgst: int, sgst: int, igst: int) -> None:
    s = split_gst(Paise(gross), "10", cust)
    assert (s.taxable, s.cgst, s.sgst, s.igst) == (taxable, cgst, sgst, igst)
    assert s.total == gross


def test_bill_of_supply() -> None:
    s = split_gst(Paise(49900), "10", "10", registered=False)
    assert (s.taxable, s.cgst, s.sgst, s.igst) == (49900, 0, 0, 0)


def test_wallet_debit_taxable() -> None:
    assert taxable_of(Paise(400)) == 339


@pytest.mark.parametrize("bad", ["", "1", "100", "ab", "٣٣"])
def test_bad_state(bad: str) -> None:
    with pytest.raises(ValueError):
        split_gst(Paise(100), "10", bad)
    with pytest.raises(ValueError):
        split_gst(Paise(100), bad, "10")
