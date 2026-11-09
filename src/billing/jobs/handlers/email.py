"""email.send: renders one billing email and hands it to the EmailSender (worker only).

Enqueued in the transaction of the triggering change (app/billing_emails.py). Five attempts with
the job runner's backoff (2, 4, 8, 16 s), then the job is dead (see runbooks/email.md to replay).
Every failed attempt counts in billing_email_failed_total{template}. A receipt or refund email
whose PDF does not exist yet raises, so it is retried instead of going out without its PDF.
"""

from typing import Any
from uuid import UUID

import structlog

from billing.adapters.email_templates import render_email
from billing.app.billing_emails import EMAIL_JOB
from billing.app.payment_links import payment_action_url
from billing.app.ports import EmailMessage
from billing.jobs.runner import JobContext, job_handler
from billing.observability import EMAIL_FAILED, EMAIL_SENT, EMAIL_SKIPPED

log = structlog.get_logger()
PAYMENT_LINK_TEMPLATES = frozenset({"payment_failed", "subscription_suspended"})


class PdfNotReady(RuntimeError):
    """The invoice PDF the email carries has not been rendered yet; the job retries."""


def pdf_filename(number: str) -> str:
    return number.replace("/", "-") + ".pdf"


@job_handler(EMAIL_JOB)
async def send_email(payload: dict[str, Any], ctx: JobContext) -> None:
    template = str(payload["template"])
    try:
        await _send(template, payload, ctx)
    except Exception:
        EMAIL_FAILED.labels(template=template).inc()
        raise


async def _pdf(ctx: JobContext, invoice_id: str) -> tuple[str, bytes, str]:
    """(invoice number, PDF bytes, kind) of a rendered invoice or credit note."""
    assert ctx.storage is not None
    async with ctx.uow_factory() as uow:
        inv = await uow.invoices.get(UUID(invoice_id))
    if inv is None:
        raise LookupError(f"invoice {invoice_id} not found")
    data = await ctx.storage.get(inv.pdf_object_key) if inv.pdf_object_key else None
    if data is None:
        raise PdfNotReady(f"the PDF of {inv.number} is not available yet")
    return inv.number, data, inv.kind


async def _send(template: str, payload: dict[str, Any], ctx: JobContext) -> None:
    if ctx.email is None or ctx.billing is None or ctx.storage is None:
        raise RuntimeError("email.send needs the email sender, billing context and storage")
    cfg = ctx.billing.config
    if not cfg.email_enabled:
        EMAIL_SKIPPED.labels(template=template, reason="disabled").inc()
        return
    async with ctx.uow_factory() as uow:
        account = await uow.accounts.get(UUID(payload["account_id"]))
    if account is None or not account.billing_email:
        EMAIL_SKIPPED.labels(template=template, reason="no_recipient").inc()
        log.info("email_skipped_no_recipient", template=template)
        return

    origin = cfg.frontend_origin.rstrip("/")
    data: dict[str, Any] = {
        **payload["data"],
        "account_email": account.billing_email,
        "support_email": cfg.support_email,
        "billing_url": f"{origin}/settings/billing",
    }
    attachments: list[tuple[str, str, bytes]] = []
    if template == "receipt":
        number, pdf, kind = await _pdf(ctx, data["invoice_id"])
        data["title"] = "Bill of supply" if kind == "bill_of_supply" else "Receipt"
        data["filename"] = pdf_filename(number)
        attachments.append((data["filename"], "application/pdf", pdf))
    elif template == "refund_issued" and data.get("credit_note_id"):
        number, pdf, _ = await _pdf(ctx, data["credit_note_id"])
        attachments.append((pdf_filename(number), "application/pdf", pdf))
    elif template in PAYMENT_LINK_TEMPLATES:
        data["pay_url"] = await _pay_url(ctx, account.owner_id, data, origin)

    rendered = render_email(template, data, simulated=cfg.simulation)
    await ctx.email.send(
        EmailMessage(
            to=account.billing_email,
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            attachments=tuple(attachments),
            headers={"X-GranthanAI-Template": template},
            idempotency_key=str(payload["key"]),
        )
    )
    EMAIL_SENT.labels(template=template).inc()


async def _pay_url(ctx: JobContext, user_id: UUID, data: dict[str, Any], origin: str) -> str:
    """Pay now: the same value /me shows as payment_action_url, as an absolute link."""
    assert ctx.billing is not None
    async with ctx.uow_factory() as uow:
        sub = await uow.subscriptions.get(UUID(data["subscription_id"]))
    gid = sub.state.gateway_subscription_id if sub else None
    url = await payment_action_url(ctx.billing.gateway, user_id, gid) if gid else None
    if url is None:
        return str(data["billing_url"])
    return str(origin + url if url.startswith("/") else url)
