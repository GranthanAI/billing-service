"""Integer money. Paise for charges, micro-rupees (1 INR = 1,000,000) for AI cost."""

from dataclasses import dataclass
from typing import NewType

Paise = NewType("Paise", int)
MicroInr = NewType("MicroInr", int)


def round_half_up(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("round_half_up needs numerator >= 0 and denominator > 0")
    return (2 * numerator + denominator) // (2 * denominator)


@dataclass(frozen=True, slots=True)
class GstSplit:
    taxable: Paise
    cgst: Paise
    sgst: Paise
    igst: Paise

    @property
    def total(self) -> Paise:
        return Paise(self.taxable + self.cgst + self.sgst + self.igst)


def _check_state(code: str) -> None:
    if len(code) != 2 or not code.isascii() or not code.isdigit():
        raise ValueError(f"invalid GST state code: {code!r}")


def taxable_of(gross: Paise) -> Paise:
    """Taxable value of a GST-inclusive amount (18%)."""
    return Paise(round_half_up(gross * 100, 118))


def display_paise(taxable: Paise, registered: bool = True) -> Paise:
    """What a user sees for a wallet amount: the taxable value plus 18% GST, rounded half up.
    Unchanged when the supplier is not GST registered (nothing was added at top-up)."""
    if not registered:
        return taxable
    return Paise(round_half_up(taxable * 118, 100))


def split_gst(
    gross: Paise, supplier_state: str, customer_state: str, registered: bool = True
) -> GstSplit:
    if gross < 0:
        raise ValueError("gross must be >= 0")
    _check_state(supplier_state)
    _check_state(customer_state)
    if not registered:
        return GstSplit(gross, Paise(0), Paise(0), Paise(0))
    taxable = taxable_of(gross)
    tax = gross - taxable
    if supplier_state == customer_state:
        cgst = tax // 2
        return GstSplit(taxable, Paise(cgst), Paise(tax - cgst), Paise(0))
    return GstSplit(taxable, Paise(0), Paise(0), Paise(tax))
