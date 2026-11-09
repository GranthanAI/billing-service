"""Picks the EmailSender from EMAIL_MODE. Only the worker calls this: the API never sends mail."""

from billing.adapters.email_console import ConsoleEmailSender
from billing.adapters.email_file import FileEmailSender
from billing.adapters.email_smtp import SmtpEmailSender
from billing.app.ports import Clock, EmailSender
from billing.settings import Settings, check_email_config


def make_email_sender(s: Settings, clock: Clock) -> EmailSender:
    check_email_config(s.email_mode, s.environment, s.smtp_host, s.email_from)
    if s.email_mode == "file":
        return FileEmailSender(
            s.email_outbox_dir, s.email_from or "billing@localhost", clock, s.email_reply_to
        )
    if s.email_mode == "smtp":
        return SmtpEmailSender(
            host=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username,
            password=s.smtp_password,
            sender=s.email_from,
            clock=clock,
            reply_to=s.email_reply_to,
            security=s.smtp_security,
        )
    return ConsoleEmailSender()
