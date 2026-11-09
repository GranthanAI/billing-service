"""Invoice PDFs: build the document, render it, store it, announce it.

`render` is safe to run twice: a second run finds the PDF already set and does nothing, so
`invoice.issued` is published once per document.
"""

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import structlog

from billing.app.billing_emails import enqueue_email, iso
from billing.app.config import BillingConfig
from billing.app.events import InvoiceIssuedEvent
from billing.app.invoice_doc import DocLine, DocTax, InvoiceDocument
from billing.app.ports import Clock, InvoiceRenderer, ObjectStorage, UnitOfWork
from billing.app.records import BillingAccount, InvoiceLineRecord, InvoiceRecord
from billing.domain.calendar import IST
from billing.domain.india import STATE_NAMES
from billing.domain.money import Paise
from billing.domain.words import format_inr, rupees_in_words

log = structlog.get_logger()

RENDER_JOB = "invoice.render"
BACKFILL_JOB = "invoice.backfill"
BACKFILL_BATCH = 500
RECEIPT_MAX_AGE = timedelta(days=7)  # a backfilled old invoice is not re-announced
SIM_WATERMARK = "SIMULATED - NOT A TAX INVOICE"
TITLES = {
    "tax_invoice": "Tax invoice",
    "bill_of_supply": "Bill of supply",
    "credit_note": "Credit note",
}


def object_key(number: str) -> str:
    """`GA/2627/000123` -> `invoices/2627/GA-2627-000123.pdf`."""
    series, fy, serial = number.split("/")
    return f"invoices/{fy}/{series}-{fy}-{serial}.pdf"


def state_label(code: str) -> str:
    return f"{STATE_NAMES.get(code, 'Unknown')} ({code})"


def _date(dt: object) -> str:
    local = dt.astimezone(IST)  # type: ignore[attr-defined]
    return f"{local.day} {local:%b} {local.year}"


def build_document(
    inv: InvoiceRecord,
    lines: list[InvoiceLineRecord],
    account: BillingAccount | None,
    config: BillingConfig,
    original: InvoiceRecord | None,
) -> InvoiceDocument:
    taxes: list[DocTax] = []
    if inv.kind != "bill_of_supply":
        for name, rate, amount in (
            ("CGST", "9%", inv.cgst_paise),
            ("SGST", "9%", inv.sgst_paise),
            ("IGST", "18%", inv.igst_paise),
        ):
            if amount:
                taxes.append(DocTax(name, rate, format_inr(amount)))
    period = ""
    if inv.period_start and inv.period_end:
        period = f"{_date(inv.period_start)} - {_date(inv.period_end)}"
    name = inv.customer_name or (account.legal_name if account else None) or "Customer"
    return InvoiceDocument(
        title=TITLES[inv.kind],
        number=inv.number,
        issued_on=_date(inv.issued_at),
        supplier_name=config.supplier_legal_name,
        supplier_address=config.supplier_address,
        supplier_gstin=config.supplier_gstin if inv.kind != "bill_of_supply" else "",
        supplier_state=state_label(inv.supplier_state_code),
        recipient_name=name,
        recipient_gstin=inv.customer_gstin or "",
        recipient_state=state_label(inv.customer_state_code),
        place_of_supply=state_label(inv.customer_state_code),
        lines=tuple(
            DocLine(
                ln.line_no, ln.description, ln.sac_code, ln.quantity, format_inr(ln.taxable_paise)
            )
            for ln in lines
        ),
        taxes=tuple(taxes),
        taxable_total=format_inr(inv.taxable_paise),
        total=format_inr(inv.total_paise),
        total_words=rupees_in_words(Paise(inv.total_paise)),
        period=period,
        original_number=original.number if original else "",
        original_date=_date(original.issued_at) if original else "",
        watermark=SIM_WATERMARK if config.simulation else "",
    )


class InvoiceService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        renderer: InvoiceRenderer,
        storage: ObjectStorage,
        clock: Clock,
        config: BillingConfig,
    ) -> None:
        self._uow = uow_factory
        self._renderer = renderer
        self._storage = storage
        self._clock = clock
        self._config = config

    async def render(self, invoice_id: UUID) -> bool:
        """True when this call rendered and announced the PDF."""
        async with self._uow() as uow:
            inv = await uow.invoices.get(invoice_id)
            if inv is None:
                raise LookupError(f"invoice {invoice_id} not found")
            if inv.pdf_object_key is not None:
                return False
            lines = await uow.invoices.lines(invoice_id)
            account = await uow.accounts.get(inv.billing_account_id)
            original = (
                await uow.invoices.get(inv.original_invoice_id) if inv.original_invoice_id else None
            )
        doc = build_document(inv, lines, account, self._config, original)
        pdf = await self._renderer.render(doc)  # outside any transaction
        key = object_key(inv.number)
        await self._storage.put(key, pdf, "application/pdf")
        async with self._uow() as uow:
            if not await uow.invoices.set_pdf(invoice_id, key):
                return False  # a concurrent render won; its event is the one
            await uow.publisher.enqueue(
                InvoiceIssuedEvent(
                    billing_account_id=inv.billing_account_id,
                    invoice_id=inv.id,
                    number=inv.number,
                    kind=inv.kind,
                    total=Paise(inv.total_paise),
                    pdf_object_key=key,
                )
            )
            if (
                inv.kind in ("tax_invoice", "bill_of_supply")
                and self._clock.now() - inv.issued_at <= RECEIPT_MAX_AGE
            ):
                await enqueue_email(
                    uow, self._config, "receipt", str(inv.id), account,
                    {
                        "invoice_id": str(inv.id),
                        "invoice_number": inv.number,
                        "kind": inv.kind,
                        "issued_at": iso(inv.issued_at),
                        "total_paise": int(inv.total_paise),
                        "description": lines[0].description if lines else "",
                        "period_start": iso(inv.period_start),
                        "period_end": iso(inv.period_end),
                    },
                    self._clock.now(),
                )  # fmt: skip
        log.info("invoice_rendered", number=inv.number, kind=inv.kind)
        return True

    async def backfill(self) -> int:
        """Queue a render for every invoice that has no PDF (one-off, e.g. after an outage)."""
        async with self._uow() as uow:
            ids = await uow.invoices.without_pdf(BACKFILL_BATCH)
            now = self._clock.now()
            for invoice_id in ids:
                await uow.jobs.enqueue(
                    RENDER_JOB, {"invoice_id": str(invoice_id)}, now, f"{RENDER_JOB}:{invoice_id}"
                )
        return len(ids)
