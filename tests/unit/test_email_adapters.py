"""P2a: the email port's adapters (console, file, smtp against a fake server) and the guards."""

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from billing.adapters.email_console import ConsoleEmailSender
from billing.adapters.email_factory import make_email_sender
from billing.adapters.email_file import FileEmailSender
from billing.adapters.email_mime import build_mime, message_id
from billing.adapters.email_smtp import SmtpEmailSender, resolve_security
from billing.app.ports import EmailMessage
from billing.settings import check_email_config
from tests.support.clock import TestClock

CLOCK = TestClock(datetime(2026, 11, 9, 10, 0, tzinfo=UTC))
SENDER = "GranthanAI <billing@granthan.ai>"
PDF = b"%PDF-1.4 fake"


def msg(key: str = "receipt:inv-1", to: str = "ada@example.com") -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Your receipt, 9 Nov",
        text="Hello Ada, thanks.",
        html="<p>Hello <b>Ada</b></p>",
        attachments=(("GA-2627-000001.pdf", "application/pdf", PDF),),
        headers={"X-Template": "receipt"},
        idempotency_key=key,
    )


# --- mime -----------------------------------------------------------------------------------


def test_message_id_is_derived_from_the_idempotency_key() -> None:
    assert message_id("k1", SENDER) == message_id("k1", SENDER)
    assert message_id("k1", SENDER) != message_id("k2", SENDER)
    assert message_id("k1", SENDER).endswith("@granthan.ai>")
    assert message_id("", SENDER) != message_id("", SENDER)  # no key: unique each time


def test_mime_has_text_html_attachment_and_headers() -> None:
    mime = build_mime(msg(), sender=SENDER, reply_to="help@granthan.ai")
    parsed = message_from_bytes(bytes(mime), policy=policy.default)
    assert parsed["From"] == SENDER and parsed["To"] == "ada@example.com"
    assert parsed["Reply-To"] == "help@granthan.ai" and parsed["X-Template"] == "receipt"
    assert parsed["Message-ID"] == message_id("receipt:inv-1", SENDER)
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None and "Hello Ada" in body.get_content()
    html = parsed.get_body(preferencelist=("html",))
    assert html is not None and "<b>Ada</b>" in html.get_content()
    (attachment,) = list(parsed.iter_attachments())
    assert attachment.get_filename() == "GA-2627-000001.pdf"
    assert attachment.get_content() == PDF


# --- console --------------------------------------------------------------------------------


async def test_console_logs_subject_redacted_recipient_body_and_attachment_sizes() -> None:
    with structlog.testing.capture_logs() as logs:
        await ConsoleEmailSender().send(msg())
    (entry,) = [e for e in logs if e["event"] == "email_console"]
    assert entry["subject"] == "Your receipt, 9 Nov" and "Hello Ada" in entry["body"]
    assert entry["attachments"] == [f"GA-2627-000001.pdf ({len(PDF)} bytes)"]
    assert "ada@example.com" not in str(entry)  # recipients are redacted


# --- file -----------------------------------------------------------------------------------


async def test_file_writes_one_eml_per_message_and_a_retry_rewrites_it(tmp_path: Path) -> None:
    sender = FileEmailSender(tmp_path / "outbox", SENDER, CLOCK)
    await sender.send(msg("receipt:a"))
    await sender.send(msg("receipt:a"))  # a retry or a replay: the same file
    await sender.send(msg("receipt:b"))
    files = sorted((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 2
    parsed = message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Your receipt, 9 Nov" and parsed["Date"]
    assert any(a.get_filename() == "GA-2627-000001.pdf" for a in parsed.iter_attachments())


# --- smtp -----------------------------------------------------------------------------------


class FakeSmtp:
    """Just enough SMTP for smtplib: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(self, *, refuse_rcpt: bool = False) -> None:
        self.refuse_rcpt = refuse_rcpt
        self.messages: list[dict[str, Any]] = []
        self.logins: list[tuple[str, str]] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        current: dict[str, Any] = {"to": []}
        await say("220 fake ESMTP")
        while line := (await reader.readline()).decode().strip():
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                writer.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
                await writer.drain()
            elif verb == "AUTH":
                user, password = base64.b64decode(line.split()[-1]).decode().split("\0")[1:]
                self.logins.append((user, password))
                await say("235 ok")
            elif verb == "MAIL":
                current = {"from": line, "to": []}
                await say("250 ok")
            elif verb == "RCPT":
                if self.refuse_rcpt:
                    await say("550 no such user")
                else:
                    current["to"].append(line)
                    await say("250 ok")
            elif verb == "DATA":
                await say("354 go")
                data = b""
                while (chunk := await reader.readline()) != b".\r\n":
                    data += chunk
                current["data"] = data
                self.messages.append(current)
                await say("250 queued")
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:
                await say("250 ok")
        writer.close()


@pytest.fixture
async def smtp() -> AsyncIterator[FakeSmtp]:
    server = FakeSmtp()
    await server.start()
    yield server
    await server.stop()


def smtp_sender(server: FakeSmtp, **kw: Any) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="127.0.0.1", port=server.port, username=kw.pop("username", ""), password="pw",
        sender=SENDER, clock=CLOCK, reply_to="help@granthan.ai", security="none", **kw,
    )  # fmt: skip


async def test_smtp_delivers_with_headers_attachment_and_login(smtp: FakeSmtp) -> None:
    await smtp_sender(smtp, username="apikey").send(msg())
    assert smtp.logins == [("apikey", "pw")]
    (sent,) = smtp.messages
    assert "billing@granthan.ai" in sent["from"] and "ada@example.com" in sent["to"][0]
    parsed = message_from_bytes(sent["data"], policy=policy.default)
    assert parsed["Message-ID"] == message_id("receipt:inv-1", SENDER)
    assert parsed["Reply-To"] == "help@granthan.ai"
    assert [a.get_content() for a in parsed.iter_attachments()] == [PDF]


async def test_smtp_retry_keeps_the_same_message_id(smtp: FakeSmtp) -> None:
    sender = smtp_sender(smtp)
    await sender.send(msg())
    await sender.send(msg())
    ids = [message_from_bytes(m["data"])["Message-ID"] for m in smtp.messages]
    assert len(ids) == 2 and ids[0] == ids[1]


async def test_smtp_failures_raise_so_the_job_retries() -> None:
    refusing = FakeSmtp(refuse_rcpt=True)
    await refusing.start()
    try:
        with pytest.raises(Exception, match="550|refused"):
            await smtp_sender(refusing).send(msg())
    finally:
        await refusing.stop()
    down = FakeSmtp()
    await down.start()
    await down.stop()  # nothing listens any more
    with pytest.raises(OSError):
        await smtp_sender(down).send(msg())


def test_smtp_security_follows_the_port() -> None:
    assert resolve_security(587, "auto") == "starttls"
    assert resolve_security(465, "auto") == "ssl"
    assert resolve_security(2525, "auto") == "none"
    assert resolve_security(587, "none") == "none"


# --- startup guards -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["console", "file"])
def test_console_and_file_are_refused_in_production(mode: str) -> None:
    with pytest.raises(ValueError, match="production"):
        check_email_config(mode, "production", "", "")
    check_email_config(mode, "staging", "", "")


def test_smtp_needs_a_host_and_a_sender() -> None:
    for host, sender in (("", "a@b.c"), ("smtp.example.com", ""), ("", "")):
        with pytest.raises(ValueError, match="SMTP_HOST"):
            check_email_config("smtp", "production", host, sender)
    check_email_config("smtp", "production", "smtp.example.com", "billing@granthan.ai")


def settings(**kw: Any) -> Any:
    base = dict(
        email_mode="console", environment="development", smtp_host="", email_from="",
        email_outbox_dir="x", email_reply_to="", smtp_port=587, smtp_username="",
        smtp_password="", smtp_security="auto",
    )  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def test_the_factory_picks_by_mode_and_applies_the_guards(tmp_path: Path) -> None:
    assert isinstance(make_email_sender(settings(), CLOCK), ConsoleEmailSender)
    file_mode = settings(email_mode="file", email_outbox_dir=str(tmp_path))
    assert isinstance(make_email_sender(file_mode, CLOCK), FileEmailSender)
    real = settings(email_mode="smtp", smtp_host="h", email_from="a@b.c", environment="production")
    assert isinstance(make_email_sender(real, CLOCK), SmtpEmailSender)
    with pytest.raises(ValueError):
        make_email_sender(settings(email_mode="console", environment="production"), CLOCK)
    with pytest.raises(ValueError):
        make_email_sender(settings(email_mode="smtp"), CLOCK)
