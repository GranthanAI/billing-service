"""EmailSender for development: logs the email. Recipients are redacted (redaction.py)."""

import structlog

from billing.app.ports import EmailMessage
from billing.redaction import redact

log = structlog.get_logger()


class ConsoleEmailSender:
    async def send(self, message: EmailMessage) -> None:
        log.info(
            "email_console",
            to=redact(message.to),
            subject=message.subject,
            body=message.text,
            attachments=[f"{name} ({len(data)} bytes)" for name, _, data in message.attachments],
            idempotency_key=message.idempotency_key,
        )
