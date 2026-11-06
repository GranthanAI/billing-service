"""Everything the invoice template prints, as plain data. The renderer decides how it looks."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocLine:
    number: int
    description: str
    sac_code: str
    quantity: int
    taxable: str  # already formatted: "422.88"


@dataclass(frozen=True, slots=True)
class DocTax:
    name: str  # CGST | SGST | IGST
    rate: str  # "9%"
    amount: str


@dataclass(frozen=True, slots=True)
class InvoiceDocument:
    title: str  # Tax invoice | Bill of supply | Credit note
    number: str
    issued_on: str  # "1 Nov 2026" (IST)
    supplier_name: str
    supplier_address: str
    supplier_gstin: str
    supplier_state: str  # "Bihar (10)"
    recipient_name: str
    recipient_gstin: str
    recipient_state: str
    place_of_supply: str
    lines: tuple[DocLine, ...]
    taxes: tuple[DocTax, ...]  # empty for a bill of supply
    taxable_total: str
    total: str
    total_words: str
    period: str = ""  # "1 Nov 2026 - 1 Dec 2026"
    original_number: str = ""  # credit notes only
    original_date: str = ""
    payment_reference: str = ""
