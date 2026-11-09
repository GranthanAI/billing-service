"""P2c: golden-file renders of every email template (text, HTML and subject), plus the rules
every email follows. Refresh the files with UPDATE_GOLDEN=1 after an intended copy change."""

import os
import re
from pathlib import Path
from typing import Any

import pytest

from billing.adapters.email_templates import inr, ist_date, render_email
from billing.app.billing_emails import TEMPLATES

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "email"
COMMON = {
    "account_email": "ada@example.com",
    "support_email": "support@granthan.ai",
    "billing_url": "https://app.granthan.ai/settings/billing",
}
PAY = "https://rzp.io/i/abc123"
DATA: dict[str, dict[str, Any]] = {
    "receipt": {
        "title": "Receipt", "invoice_number": "GA/2627/000001", "filename": "GA-2627-000001.pdf",
        "issued_at": "2026-11-09T04:45:00+00:00", "total_paise": 14990000,
        "description": "GranthanAI Max · yearly · 9 Nov 2026 – 9 Nov 2027",
        "period_start": "2026-11-09T04:45:00+00:00", "period_end": "2027-11-09T04:45:00+00:00",
    },
    "payment_failed": {
        "plan_code": "pro", "amount_paise": 49900, "pay_url": PAY,
        "grace_until": "2026-12-04T04:45:00+00:00", "next_retry_at": "2026-12-02T04:45:00+00:00",
    },
    "subscription_suspended": {"plan_code": "max", "pay_url": PAY},
    "renewal_upcoming": {
        "plan_code": "pro", "amount_paise": 49900, "charge_at": "2026-12-01T04:45:00+00:00",
    },
    "subscription_cancelled": {
        "plan_code": "pro", "access_until": "2026-12-01T04:45:00+00:00", "by": "user",
    },
    "refund_issued": {
        "amount_paise": 24950, "credit_note_id": "x", "credit_note_number": "CN/2627/000001",
    },
    "credits_expiring": {"remaining_paise": 199000, "expires_at": "2027-10-31T18:30:00+00:00"},
}  # fmt: skip


def render(template: str, *, simulated: bool = False, **extra: Any) -> Any:
    return render_email(template, {**COMMON, **DATA[template], **extra}, simulated=simulated)


def check(name: str, suffix: str, actual: str) -> None:
    path = GOLDEN / f"{name}.{suffix}"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden file {path}; run with UPDATE_GOLDEN=1"
    assert actual == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("simulated", [False, True], ids=["live", "simulated"])
def test_golden_render(template: str, simulated: bool) -> None:
    mail = render(template, simulated=simulated)
    name = f"{template}{'.simulated' if simulated else ''}"
    check(name, "subject.txt", mail.subject + "\n")
    check(name, "txt", mail.text)
    check(name, "html", mail.html)


def test_every_template_has_a_golden_set() -> None:
    assert set(DATA) == set(TEMPLATES)


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_email_carries_the_standard_parts(template: str) -> None:
    mail = render(template)
    for part in (mail.text, mail.html):
        assert "GranthanAI" in part and "ada@example.com" in part
        assert "support@granthan.ai" in part
        assert "You're receiving this because you have a GranthanAI billing account." in part
    assert "simulated" not in mail.text.lower() and not mail.subject.startswith("[SIMULATED]")
    assert (
        "₹" in mail.text
        and "₹" in mail.html
        or template
        in (
            "subscription_suspended",
            "subscription_cancelled",
        )
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_simulated_emails_are_marked(template: str) -> None:
    mail = render(template, simulated=True)
    assert mail.subject.startswith("[SIMULATED] ")
    assert mail.text.startswith("This is a simulated payment. No money moved.")
    assert "This is a simulated payment. No money moved." in mail.html


@pytest.mark.parametrize("template", TEMPLATES)
def test_html_is_plain_single_column_with_no_remote_assets(template: str) -> None:
    html = render(template).html
    assert "max-width:600px" in html
    assert not re.search(r"<img|@import|fonts\.googleapis|<link |<script|url\(", html)
    links = set(re.findall(r'(?:href|src)="([^"]+)"', html))
    assert links <= {PAY, COMMON["billing_url"], "mailto:support@granthan.ai"}


def test_no_link_carries_a_token_or_presigned_url() -> None:
    for template in TEMPLATES:
        mail = render(template)
        assert not re.search(r"[?&](t|token|signature|expires|X-Amz)", mail.text + mail.html, re.I)


def test_money_uses_indian_grouping_and_the_rupee_sign() -> None:
    assert inr(14990000) == "₹1,49,900.00"
    assert inr(49900) == "₹499.00" and inr(5) == "₹0.05" and inr(10000000) == "₹1,00,000.00"
    assert "₹1,49,900.00" in render("receipt").text


def test_dates_are_in_ist() -> None:
    assert ist_date("2026-11-09T04:45:00+00:00") == "9 Nov 2026"
    assert ist_date("2026-10-31T18:30:00+00:00") == "1 Nov 2026"  # already the next day in IST
    assert "1 Nov 2027" in render("credits_expiring").text


def test_html_is_escaped_but_text_is_not() -> None:
    mail = render("receipt", description="<b>Pro</b> & more")
    assert "&lt;b&gt;Pro&lt;/b&gt; &amp; more" in mail.html and "<b>Pro</b>" not in mail.html
    assert "<b>Pro</b> & more" in mail.text


def test_a_bill_of_supply_is_titled_so() -> None:
    mail = render("receipt", title="Bill of supply")
    assert mail.subject.startswith("Bill of supply GA/2627/000001")
    assert "The bill of supply is attached" in mail.text


def test_the_retry_sentence_appears_only_when_the_provider_has_a_next_attempt() -> None:
    assert "try the charge again" in render("payment_failed").text
    assert "try the charge again" not in render("payment_failed", next_retry_at=None).text


def test_a_mandate_cancelled_in_the_bank_app_reads_differently() -> None:
    assert "bank or UPI app" in render("subscription_cancelled", by="payment_app").text
    assert "bank or UPI app" not in render("subscription_cancelled").text
