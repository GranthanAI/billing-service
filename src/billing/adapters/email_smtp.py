"""EmailSender over SMTP with the standard library (smtplib in a worker thread).

STARTTLS on port 587, implicit SSL on 465, a 15 second timeout. `security` overrides the choice by
port (auto | starttls | ssl | none); `none` is for a local test server.
"""

import asyncio
import smtplib
import ssl
from email.utils import format_datetime

import structlog

from billing.adapters.email_mime import build_mime
from billing.app.ports import Clock, EmailMessage
from billing.redaction import redact

log = structlog.get_logger()
TIMEOUT_SECONDS = 15.0


def resolve_security(port: int, security: str) -> str:
    if security != "auto":
        return security
    return "ssl" if port == 465 else "starttls" if port == 587 else "none"


class SmtpEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        clock: Clock,
        reply_to: str = "",
        security: str = "auto",
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._reply_to = sender, reply_to
        self._security = resolve_security(port, security)
        self._clock = clock
        self._timeout = timeout

    def _send_sync(self, message: EmailMessage) -> None:
        mime = build_mime(
            message,
            sender=self._sender,
            reply_to=self._reply_to,
            now_rfc2822=format_datetime(self._clock.now()),
        )
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if self._security == "ssl":
            client = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with client:
            client.ehlo()
            if self._security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self._username:
                client.login(self._username, self._password)
            client.send_message(mime, from_addr=self._sender, to_addrs=[message.to])

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, message)  # raises on failure: the job retries
        log.info("email_smtp_sent", to=redact(message.to), host=self._host)
