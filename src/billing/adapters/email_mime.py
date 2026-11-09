"""Builds the MIME message every email adapter sends or stores."""

import hashlib
import re
import uuid
from email.message import EmailMessage as MimeMessage
from email.utils import format_datetime, formataddr, parseaddr

from billing.app.ports import EmailMessage


def sender_domain(sender: str) -> str:
    address = parseaddr(sender)[1]
    return address.rpartition("@")[2] or "billing.invalid"


def message_id(idempotency_key: str, sender: str) -> str:
    """Derived from the idempotency key, so a retried send keeps the same Message-ID."""
    local = (
        hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        if idempotency_key
        else uuid.uuid4().hex
    )
    return f"<{local}@{sender_domain(sender)}>"


def build_mime(
    message: EmailMessage, *, sender: str, reply_to: str = "", now_rfc2822: str | None = None
) -> MimeMessage:
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Message-ID"] = message_id(message.idempotency_key, sender)
    if reply_to:
        mime["Reply-To"] = reply_to
    if now_rfc2822:
        mime["Date"] = now_rfc2822
    for name, value in message.headers.items():
        mime[name] = value
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    for filename, content_type, data in message.attachments:
        maintype, _, subtype = content_type.partition("/")
        mime.add_attachment(
            data, maintype=maintype or "application", subtype=subtype or "octet-stream",
            filename=filename,
        )  # fmt: skip
    return mime


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:limit] or "email"


__all__ = ["build_mime", "format_datetime", "formataddr", "message_id", "slug"]
