"""EmailSender that writes one .eml per message, so it opens by double-clicking on Windows.

The file name comes from the Message-ID, so sending the same message twice (a retry, a replay)
rewrites the same file instead of adding a second one.
"""

import asyncio
import hashlib
from email.utils import format_datetime
from pathlib import Path

import structlog

from billing.adapters.email_mime import build_mime, message_id, slug
from billing.app.ports import Clock, EmailMessage
from billing.redaction import redact

log = structlog.get_logger()


class FileEmailSender:
    def __init__(
        self, directory: str | Path, sender: str, clock: Clock, reply_to: str = ""
    ) -> None:
        self._dir = Path(directory)
        self._sender = sender
        self._clock = clock
        self._reply_to = reply_to

    def path_for(self, message: EmailMessage) -> Path:
        mid = message_id(message.idempotency_key, self._sender)
        return (
            self._dir
            / f"{slug(message.subject)}-{hashlib.sha256(mid.encode()).hexdigest()[:10]}.eml"
        )

    def _write(self, message: EmailMessage) -> Path:
        mime = build_mime(
            message,
            sender=self._sender,
            reply_to=self._reply_to,
            now_rfc2822=format_datetime(self._clock.now()),
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(message)
        path.write_bytes(bytes(mime))
        return path

    async def send(self, message: EmailMessage) -> None:
        path = await asyncio.to_thread(self._write, message)
        log.info("email_file_written", to=redact(message.to), path=str(path))
