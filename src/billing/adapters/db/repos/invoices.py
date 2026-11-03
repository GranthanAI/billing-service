from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import invoice_counters, invoice_lines
from billing.adapters.db.tables import invoices as t
from billing.app.records import InvoiceLineRecord, InvoiceRecord
from billing.domain.money import Paise


def _record(r: Any) -> InvoiceRecord:
    return InvoiceRecord(
        id=r.id,
        number=r.number,
        kind=r.kind,
        billing_account_id=r.billing_account_id,
        issued_at=r.issued_at,
        customer_state_code=r.customer_state_code,
        supplier_state_code=r.supplier_state_code,
        taxable_paise=Paise(r.taxable_paise),
        cgst_paise=Paise(r.cgst_paise),
        sgst_paise=Paise(r.sgst_paise),
        igst_paise=Paise(r.igst_paise),
        total_paise=Paise(r.total_paise),
        status=r.status,
        original_invoice_id=r.original_invoice_id,
        payment_id=r.payment_id,
        period_start=r.period_start,
        period_end=r.period_end,
        customer_name=r.customer_name,
        customer_gstin=r.customer_gstin,
        pdf_object_key=r.pdf_object_key,
    )


class SqlInvoiceRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def allocate_number(self, series: str, financial_year: str) -> str:
        """Gapless: the counter row is locked and incremented in the caller's transaction, so a
        rollback also rolls the number back. A Postgres SEQUENCE would leave gaps."""
        await self._c.execute(
            pg_insert(invoice_counters)
            .values(series=series, financial_year=financial_year)
            .on_conflict_do_nothing()
        )
        result = await self._c.execute(
            sa.update(invoice_counters)
            .where(
                invoice_counters.c.series == series,
                invoice_counters.c.financial_year == financial_year,
            )
            .values(next_number=invoice_counters.c.next_number + 1)
            .returning(invoice_counters.c.next_number - 1)
        )
        n: int = result.scalar_one()
        return f"{series}/{financial_year}/{n:06d}"

    async def insert(self, invoice: InvoiceRecord, lines: Sequence[InvoiceLineRecord]) -> None:
        await self._c.execute(
            sa.insert(t).values(
                id=invoice.id,
                number=invoice.number,
                kind=invoice.kind,
                original_invoice_id=invoice.original_invoice_id,
                billing_account_id=invoice.billing_account_id,
                payment_id=invoice.payment_id,
                issued_at=invoice.issued_at,
                period_start=invoice.period_start,
                period_end=invoice.period_end,
                customer_name=invoice.customer_name,
                customer_gstin=invoice.customer_gstin,
                customer_state_code=invoice.customer_state_code,
                supplier_state_code=invoice.supplier_state_code,
                taxable_paise=invoice.taxable_paise,
                cgst_paise=invoice.cgst_paise,
                sgst_paise=invoice.sgst_paise,
                igst_paise=invoice.igst_paise,
                total_paise=invoice.total_paise,
                status=invoice.status,
                pdf_object_key=invoice.pdf_object_key,
            )
        )
        if lines:
            await self._c.execute(
                sa.insert(invoice_lines),
                [
                    {
                        "invoice_id": ln.invoice_id,
                        "line_no": ln.line_no,
                        "description": ln.description,
                        "sac_code": ln.sac_code,
                        "quantity": ln.quantity,
                        "unit_price_paise": ln.unit_price_paise,
                        "taxable_paise": ln.taxable_paise,
                    }
                    for ln in lines
                ],
            )

    async def set_pdf(self, invoice_id: UUID, object_key: str) -> None:
        await self._c.execute(
            sa.update(t).where(t.c.id == invoice_id).values(pdf_object_key=object_key)
        )

    async def mark_paid(self, invoice_id: UUID) -> None:
        await self._c.execute(sa.update(t).where(t.c.id == invoice_id).values(status="paid"))

    async def get(self, invoice_id: UUID) -> InvoiceRecord | None:
        r = (await self._c.execute(sa.select(t).where(t.c.id == invoice_id))).first()
        return _record(r) if r else None

    async def by_payment(self, payment_id: UUID) -> InvoiceRecord | None:
        q = sa.select(t).where(t.c.payment_id == payment_id, t.c.kind != "credit_note")
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None
