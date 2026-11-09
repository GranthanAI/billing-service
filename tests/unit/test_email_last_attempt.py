"""email.send: a missing PDF retries, and on the last attempt the email goes out without it."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from billing.jobs.handlers.email import NO_PDF_LINE, PdfNotReady, send_email
from billing.jobs.runner import JobContext

ACCOUNT = SimpleNamespace(billing_email="ada@example.com", owner_id=uuid4())
INVOICE = SimpleNamespace(number="CN/1", pdf_object_key=None, kind="credit_note")


class _Repo:
    def __init__(self, row):
        self._row = row

    async def get(self, _id):
        return self._row


class _Uow:
    accounts = _Repo(ACCOUNT)
    invoices = _Repo(INVOICE)


@asynccontextmanager
async def _uow():
    yield _Uow()


class _Email:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def _ctx(last_attempt: bool):
    cfg = SimpleNamespace(
        email_enabled=True, frontend_origin="https://app.granthan.ai",
        support_email="support@granthan.ai", simulation=False,
    )  # fmt: skip
    email = _Email()
    ctx = JobContext(
        _uow, None, None, billing=SimpleNamespace(config=cfg), email=email,  # type: ignore[arg-type]
        storage=SimpleNamespace(), last_attempt=last_attempt,  # type: ignore[arg-type]
    )  # fmt: skip
    return ctx, email


PAYLOAD = {
    "template": "refund_issued", "key": "refund_issued:r1", "account_id": str(uuid4()),
    "data": {"amount_paise": 24950, "credit_note_id": str(uuid4()),
             "credit_note_number": "CN/1"},
}  # fmt: skip


async def test_earlier_attempt_retries_when_pdf_missing() -> None:
    ctx, email = _ctx(last_attempt=False)
    with pytest.raises(PdfNotReady):
        await send_email(PAYLOAD, ctx)
    assert email.sent == []


async def test_last_attempt_sends_without_pdf_and_with_the_line() -> None:
    ctx, email = _ctx(last_attempt=True)
    await send_email(PAYLOAD, ctx)
    (msg,) = email.sent
    line = NO_PDF_LINE.format(document="credit note")
    assert line == "Your credit note will be available in Settings → Billing"
    assert msg.attachments == () and line in msg.text and line in msg.html
