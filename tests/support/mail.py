"""A recording EmailSender for tests: keeps what was sent, and can be told to fail."""

from billing.app.ports import EmailMessage


class RecordingEmail:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_times = 0  # the next N sends raise
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("smtp is down")
        self.sent.append(message)

    def templates(self) -> list[str]:
        return [m.headers["X-GranthanAI-Template"] for m in self.sent]

    def of(self, template: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.headers["X-GranthanAI-Template"] == template]
