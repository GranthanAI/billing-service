"""P1c: simulated invoices carry the watermark; real ones do not."""

import jinja2

from billing.adapters.pdf import TEMPLATES, WeasyPrintRenderer
from billing.app.invoice_doc import DocLine, DocTax, InvoiceDocument
from billing.app.invoices import SIM_WATERMARK


def doc(watermark: str = "") -> InvoiceDocument:
    return InvoiceDocument(
        title="Tax invoice", number="SI/2627/000001", issued_on="1 Nov 2026",
        supplier_name="GranthanAI", supplier_address="", supplier_gstin="",
        supplier_state="Bihar (10)",
        recipient_name="A", recipient_gstin="", recipient_state="Bihar (10)",
        place_of_supply="Bihar (10)", lines=(DocLine(1, "Pro", "998319", 1, "422.88"),),
        taxes=(DocTax("CGST", "9%", "38.06"), DocTax("SGST", "9%", "38.06")),
        taxable_total="422.88", total="499.00", total_words="Four hundred ninety-nine",
        watermark=watermark,
    )  # fmt: skip


def html(d: InvoiceDocument) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        autoescape=jinja2.select_autoescape(["html"]),
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template("invoice.html").render(d=d)


def test_the_watermark_text_is_exact() -> None:
    assert SIM_WATERMARK == "SIMULATED - NOT A TAX INVOICE"


def test_a_simulated_invoice_prints_the_watermark() -> None:
    assert SIM_WATERMARK in html(doc(SIM_WATERMARK))
    assert "SIMULATED" not in html(doc())


def test_the_watermarked_invoice_still_renders_to_pdf() -> None:
    assert WeasyPrintRenderer().render_sync(doc(SIM_WATERMARK)).startswith(b"%PDF")
