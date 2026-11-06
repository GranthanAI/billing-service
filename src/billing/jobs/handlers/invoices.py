"""invoice.render and invoice.backfill."""

from typing import Any
from uuid import UUID

from billing.jobs.runner import JobContext, job_handler


def _service(ctx: JobContext):  # type: ignore[no-untyped-def]
    if ctx.invoices is None:
        raise RuntimeError("invoice jobs need an InvoiceService in the job context")
    return ctx.invoices


@job_handler("invoice.render")
async def invoice_render(payload: dict[str, Any], ctx: JobContext) -> None:
    await _service(ctx).render(UUID(str(payload["invoice_id"])))


@job_handler("invoice.backfill")
async def invoice_backfill(payload: dict[str, Any], ctx: JobContext) -> None:
    """One-off: queue a render for every invoice that never got a PDF."""
    await _service(ctx).backfill()
