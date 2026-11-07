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
        subscription_id=r.subscription_id,
        seat_change_to=r.seat_change_to,
        payment_link_id=r.payment_link_id,
        payment_link_url=r.payment_link_url,
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
                subscription_id=invoice.subscription_id,
                seat_change_to=invoice.seat_change_to,
                payment_link_id=invoice.payment_link_id,
                payment_link_url=invoice.payment_link_url,
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

    async def set_pdf(self, invoice_id: UUID, object_key: str) -> bool:
        result = await self._c.execute(
            sa.update(t)
            .where(t.c.id == invoice_id, t.c.pdf_object_key.is_(None))
            .values(pdf_object_key=object_key)
        )
        return result.rowcount == 1

    async def lines(self, invoice_id: UUID) -> list[InvoiceLineRecord]:
        q = (
            sa.select(invoice_lines)
            .where(invoice_lines.c.invoice_id == invoice_id)
            .order_by(invoice_lines.c.line_no)
        )
        return [
            InvoiceLineRecord(
                invoice_id=r.invoice_id,
                line_no=r.line_no,
                description=r.description,
                sac_code=r.sac_code,
                quantity=r.quantity,
                unit_price_paise=Paise(r.unit_price_paise),
                taxable_paise=Paise(r.taxable_paise),
            )
            for r in (await self._c.execute(q)).all()
        ]

    async def without_pdf(self, limit: int) -> list[UUID]:
        q = (
            sa.select(t.c.id)
            .where(t.c.pdf_object_key.is_(None))
            .order_by(t.c.issued_at, t.c.number)
            .limit(limit)
        )
        return [r[0] for r in (await self._c.execute(q)).all()]

    async def for_account(self, billing_account_id: UUID, limit: int) -> list[InvoiceRecord]:
        q = (
            sa.select(t)
            .where(t.c.billing_account_id == billing_account_id)
            .order_by(t.c.issued_at.desc(), t.c.number.desc())
            .limit(limit)
        )
        return [_record(r) for r in (await self._c.execute(q)).all()]

    async def mark_paid(self, invoice_id: UUID, payment_id: UUID | None = None) -> None:
        values: dict[str, Any] = {"status": "paid"}
        if payment_id is not None:
            values["payment_id"] = payment_id
        await self._c.execute(sa.update(t).where(t.c.id == invoice_id).values(**values))

    async def set_payment_link(self, invoice_id: UUID, link_id: str, url: str) -> None:
        await self._c.execute(
            sa.update(t)
            .where(t.c.id == invoice_id)
            .values(payment_link_id=link_id, payment_link_url=url)
        )

    async def by_payment_link(self, link_id: str) -> InvoiceRecord | None:
        r = (await self._c.execute(sa.select(t).where(t.c.payment_link_id == link_id))).first()
        return _record(r) if r else None

    async def for_period(self, subscription_id: UUID, period_start: Any) -> InvoiceRecord | None:
        """The (non true-up) invoice of a subscription for the period starting at `period_start`."""
        q = sa.select(t).where(
            t.c.subscription_id == subscription_id,
            t.c.period_start == period_start,
            t.c.seat_change_to.is_(None),
            t.c.kind != "credit_note",
        )
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None

    async def unpaid_for_subscription(self, subscription_id: UUID) -> list[InvoiceRecord]:
        q = (
            sa.select(t)
            .where(
                t.c.subscription_id == subscription_id,
                t.c.status == "issued",
                t.c.kind != "credit_note",
            )
            .order_by(t.c.period_start, t.c.issued_at)
        )
        return [_record(r) for r in (await self._c.execute(q)).all()]

    async def get(self, invoice_id: UUID) -> InvoiceRecord | None:
        r = (await self._c.execute(sa.select(t).where(t.c.id == invoice_id))).first()
        return _record(r) if r else None

    async def credited_taxable(self, payment_id: UUID) -> Paise:
        q = sa.select(sa.func.coalesce(sa.func.sum(t.c.taxable_paise), 0)).where(
            t.c.payment_id == payment_id, t.c.kind == "credit_note"
        )
        return Paise(int((await self._c.execute(q)).scalar_one()))

    async def by_payment(self, payment_id: UUID) -> InvoiceRecord | None:
        q = sa.select(t).where(t.c.payment_id == payment_id, t.c.kind != "credit_note")
        r = (await self._c.execute(q)).first()
        return _record(r) if r else None
