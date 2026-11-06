"""Amounts in words for invoices, with Indian digit grouping (lakh, crore)."""

from billing.domain.money import Paise

_ONES = [
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = ["_", "_", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def number_in_words(n: int) -> str:
    """A whole number with Indian grouping: crore, lakh, thousand."""
    if n < 0:
        raise ValueError("negative amounts are written as a credit note, not in negative words")
    if n == 0:
        return "Zero"
    parts = []
    for unit, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        count, n = divmod(n, unit)
        if count:
            parts.append(f"{number_in_words(count)} {name}")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def rupees_in_words(amount: Paise) -> str:
    """'Rupees Four Hundred Ninety Nine and Fifty Paise Only'."""
    rupees, paise = divmod(int(amount), 100)
    text = f"Rupees {number_in_words(rupees)}"
    if paise:
        text += f" and {number_in_words(paise)} Paise"
    return text + " Only"


def format_inr(amount: Paise) -> str:
    """'1,23,456.78': Indian digit grouping, two decimals, no currency symbol."""
    rupees, paise = divmod(abs(int(amount)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    sign = "-" if amount < 0 else ""
    return f"{sign}{digits}.{paise:02d}"
