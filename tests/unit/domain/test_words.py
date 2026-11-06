import pytest

from billing.domain import ledger
from billing.domain.money import Paise
from billing.domain.words import number_in_words, rupees_in_words


@pytest.mark.parametrize(
    "paise,words",
    [
        (49900, "Rupees Four Hundred Ninety Nine Only"),
        (149900, "Rupees One Thousand Four Hundred Ninety Nine Only"),
        (24950, "Rupees Two Hundred Forty Nine and Fifty Paise Only"),
        (1499000, "Rupees Fourteen Thousand Nine Hundred Ninety Only"),
        (5, "Rupees Zero and Five Paise Only"),
        (10_000_000, "Rupees One Lakh Only"),
        (123_456_700, "Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven Only"),
        (1_000_000_000, "Rupees One Crore Only"),
    ],
)
def test_amounts_in_words(paise: int, words: str) -> None:
    assert rupees_in_words(Paise(paise)) == words


def test_numbers_and_errors() -> None:
    assert number_in_words(0) == "Zero"
    assert number_in_words(100) == "One Hundred"
    assert number_in_words(21_00_00_000) == "Twenty One Crore"
    with pytest.raises(ValueError):
        number_in_words(-1)


def test_refund_settlement_moves_cash_out_of_the_bank() -> None:
    tx = ledger.refund_settlement(Paise(24950), "rs:1")
    assert [(p.account_code, p.amount) for p in tx.postings] == [
        (ledger.CLEARING, 24950),
        (ledger.BANK, -24950),
    ]
    assert tx.kind is ledger.LedgerKind.SETTLEMENT
    with pytest.raises(ValueError):
        ledger.refund_settlement(Paise(0), "rs:2")
