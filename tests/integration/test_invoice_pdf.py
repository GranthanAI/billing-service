"""Invoice PDFs: rendered for real (WeasyPrint), read back with pypdf, announced once."""

import io
import json
from datetime import UTC, datetime

import pypdf
import pytest

from billing.domain.catalog import Interval
from billing.domain.money import Paise
from billing.domain.words import format_inr
from tests.integration.billing_env import MAX_MONTH, PRO_MONTH, Env, config

KEY = "invoices/2627/GA-2627-000001.pdf"


def text_of(env: Env, key: str) -> str:
    data, content_type = env.storage.objects[key]
    assert content_type == "application/pdf" and data.startswith(b"%PDF")
    reader = pypdf.PdfReader(io.BytesIO(data))
    return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)


async def issued_events(env: Env) -> list[dict[str, object]]:
    rows = await env.sql("SELECT payload FROM billing.outbox WHERE event_type = 'invoice.issued'")
    return [json.loads(bytes(r[0])) for r in rows]


def test_indian_grouping() -> None:
    assert format_inr(Paise(0)) == "0.00"
    assert format_inr(Paise(5)) == "0.05"
    assert format_inr(Paise(49900)) == "499.00"
    assert format_inr(Paise(149900)) == "1,499.00"
    assert format_inr(Paise(12_345_678)) == "1,23,456.78"
    assert format_inr(Paise(1_000_000_000)) == "1,00,00,000.00"
    assert format_inr(Paise(-24950)) == "-249.50"


async def test_a_tax_invoice_pdf_has_every_required_field(env: Env) -> None:
    env.with_config(
        config(
            supplier_legal_name="GranthanAI Private Limited",
            supplier_address="12 Fraser Road, Patna 800001",
        )
    )
    await env.service.checkout(
        env.user, "pro", Interval.MONTH, "10", "u@example.com", "Acme Learning", "10ABCDE1234F1Z5"
    )
    sub = next(iter(env.gateway.subscriptions))
    auth, charged, _, _ = env.gateway.pay_checkout(sub, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    assert KEY not in env.storage.objects  # rendered by a job, not in the charge transaction
    await env.run_jobs()

    text = text_of(env, KEY)
    for expected in (
        "Tax invoice",
        "GA/2627/000001",
        "1 Nov 2026",  # date, IST
        "GranthanAI Private Limited",
        "12 Fraser Road, Patna 800001",
        "GSTIN: 10AAAAA0000A1Z5",  # the supplier
        "Acme Learning",
        "GSTIN: 10ABCDE1234F1Z5",  # the recipient
        "Place of supply: Bihar (10)",
        "GranthanAI Pro · monthly · 1 Nov 2026 – 1 Dec 2026",
        "998319",
        "422.88",  # taxable value
        "CGST 9% 38.06",
        "SGST 9% 38.06",
        "Total (INR) 499.00",
        "Rupees Four Hundred Ninety Nine Only",
        "Computer-generated document; no signature required.",
    ):
        assert expected in text, expected
    assert "IGST" not in text

    (event,) = await issued_events(env)
    assert event["number"] == "GA/2627/000001" and event["kind"] == "tax_invoice"
    assert event["total_paise"] == PRO_MONTH and event["pdf_object_key"] == KEY
    assert await env.scalar("SELECT pdf_object_key FROM billing.invoices") == KEY


async def test_another_state_shows_igst(env: Env) -> None:
    await env.service.checkout(env.user, "pro", Interval.MONTH, "29", "u@example.com")
    sub = next(iter(env.gateway.subscriptions))
    auth, charged, _, _ = env.gateway.pay_checkout(sub, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    await env.run_jobs()
    text = text_of(env, KEY)
    assert "IGST 18% 76.12" in text and "CGST" not in text
    assert "Place of supply: Karnataka (29)" in text


async def test_a_credit_note_names_the_invoice_it_reverses(env: Env) -> None:
    from datetime import timedelta

    options = await env.activate()
    env.clock.set(datetime(2026, 11, 16, 4, 45, tzinfo=UTC))
    up = await env.service.change_plan(env.user, "max", Interval.MONTH)
    auth, charged, _, _ = env.gateway.pay_checkout(up.gateway_subscription_id, MAX_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    await env.run_jobs()  # cancels the old mandate, refunds, issues the credit note
    await env.run_jobs()  # renders the three documents
    note = text_of(env, "invoices/2627/CN-2627-000001.pdf")
    for expected in (
        "Credit note",
        "CN/2627/000001",
        "AGAINST INVOICE GA/2627/000001 dated 1 Nov 2026",
        "Total (INR) 249.50",
        "CGST 9% 19.03",
        "Rupees Two Hundred Forty Nine and Fifty Paise Only",
    ):
        assert expected in note, expected
    numbers = sorted((await issued_events(env)), key=lambda e: str(e["number"]))
    assert [e["number"] for e in numbers] == [
        "CN/2627/000001",
        "GA/2627/000001",
        "GA/2627/000002",
    ]
    assert options is not None and timedelta(0) == timedelta(0)


async def test_a_bill_of_supply_has_no_tax_and_no_gstin(env: Env) -> None:
    env.with_config(config(gst_registered=False))
    await env.activate()
    await env.run_jobs()
    text = text_of(env, "invoices/2627/BS-2627-000001.pdf")
    assert "Bill of supply" in text and "no tax is charged" in text
    assert "CGST" not in text and "IGST" not in text and "GSTIN: 10AAAAA0000A1Z5" not in text
    assert "Total (INR) 499.00" in text


async def test_rendering_twice_announces_once_and_backfill_finds_the_missing(env: Env) -> None:
    await env.activate()
    invoice_id = await env.scalar("SELECT id FROM billing.invoices")
    assert await env.invoices.render(invoice_id) is True  # type: ignore[arg-type]
    assert await env.invoices.render(invoice_id) is False  # type: ignore[arg-type]
    assert len(await issued_events(env)) == 1

    other = env.user.__class__(int=env.user.int ^ 1)
    options = await env.checkout(user=other)
    auth, charged, _, _ = env.gateway.pay_checkout(options.gateway_subscription_id, PRO_MONTH)
    await env.deliver(auth, charged)
    await env.process()
    await env.sql("SELECT 1")
    # drop the render job the charge queued, to simulate a lost one
    async with env.migrator_engine.begin() as conn:
        import sqlalchemy as sa

        await conn.execute(sa.text("DELETE FROM billing.jobs WHERE kind = 'invoice.render'"))
    assert await env.invoices.backfill() == 1
    await env.run_jobs()
    assert (
        await env.scalar("SELECT count(*) FROM billing.invoices WHERE pdf_object_key IS NULL") == 0
    )
    assert len(await issued_events(env)) == 2
    assert await env.invoices.backfill() == 0


async def test_rendering_a_missing_invoice_fails(env: Env) -> None:
    from uuid import uuid4

    with pytest.raises(LookupError):
        await env.invoices.render(uuid4())
