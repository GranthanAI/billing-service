"""P2b: every email trigger queues exactly one email.send job in its own transaction, the job
sends once, and a missing PDF or a failing sender delays or kills the job instead of losing mail."""

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from billing.app.admin import Staff
from billing.app.billing_emails import EMAIL_JOB, enqueue_email
from billing.domain.money import Paise
from billing.observability import EMAIL_FAILED, EMAIL_SKIPPED
from tests.integration.billing_env import Env
from tests.integration.test_lifecycle import DEC1

STAFF = Staff(uuid4(), "billing_admin")


async def jobs_of(env: Env, key_prefix: str) -> int:
    return int(
        await env.scalar(
            "SELECT count(*) FROM billing.jobs WHERE kind = :k AND dedupe_key LIKE :p",
            k=EMAIL_JOB,
            p=f"{key_prefix}%",
        )  # type: ignore[arg-type]
    )


def counter(metric: object, **labels: str) -> float:
    return float(metric.labels(**labels)._value.get())  # type: ignore[attr-defined]


async def settle(env: Env) -> None:
    await env.process()
    await env.run_jobs()


# --- receipt ----------------------------------------------------------------------------------


async def test_the_receipt_carries_the_invoice_pdf_and_is_sent_once(env: Env) -> None:
    options = await env.checkout()
    authenticated, charged, _, _ = env.gateway.pay_checkout(options.gateway_subscription_id, 49900)
    await env.deliver(authenticated, charged)
    await env.process()
    assert env.mailbox.sent == []  # no PDF yet, so no job either
    await env.run_jobs()  # renders the invoice, which queues the receipt, which is sent
    (mail,) = env.mailbox.of("receipt")
    assert mail.to == "user@example.com" and "GA/2627/000001" in mail.subject
    ((filename, content_type, data),) = mail.attachments
    assert (filename, content_type) == ("GA-2627-000001.pdf", "application/pdf")
    assert data.startswith(b"%PDF")
    invoice_id = await env.scalar("SELECT id FROM billing.invoices")
    assert mail.idempotency_key == f"receipt:{invoice_id}"
    assert "₹499.00" in mail.text and "Receipt" in mail.text

    # the same webhooks again, and every job again: no second email
    await env.deliver(authenticated, charged)
    await env.process()
    await env.run_job("invoice.render")
    await env.run_jobs()
    assert len(env.mailbox.of("receipt")) == 1
    assert await jobs_of(env, "receipt:") == 1


async def test_a_bill_of_supply_uses_the_receipt_template_titled_so(env: Env) -> None:
    from billing.app.config import BillingConfig

    cfg: BillingConfig = replace(env.bctx.config, gst_registered=False)
    env.with_config(cfg)
    await env.activate()
    await env.run_jobs()
    (mail,) = env.mailbox.of("receipt")
    assert "Bill of supply" in mail.text and "BS/2627" in mail.subject
    assert mail.attachments[0][0].startswith("BS-2627-")


async def test_a_missing_pdf_delays_the_receipt_instead_of_sending_without_it(env: Env) -> None:
    await env.activate()
    invoice_id = UUID(str(await env.scalar("SELECT id FROM billing.invoices")))
    assert await env.invoices.render(invoice_id)  # the job is queued, not yet run
    key, stored = next(iter(env.storage.objects.items()))
    del env.storage.objects[key]  # the object store lost it (or has not replicated yet)

    await env.runner.run_pending()
    assert env.mailbox.sent == []  # it did not go out without its PDF
    row = (
        await env.sql(
            "SELECT status, attempts, last_error FROM billing.jobs WHERE kind = :k", k=EMAIL_JOB
        )
    )[0]
    assert (row[0], row[1]) == ("queued", 1) and "not available yet" in str(row[2])

    env.storage.objects[key] = stored  # it arrives
    env.clock.advance(seconds=30)
    await env.run_jobs()
    assert len(env.mailbox.of("receipt")) == 1


async def test_a_failing_sender_retries_five_times_then_the_job_is_dead(env: Env) -> None:
    await env.activate()
    env.mailbox.fail_times = 99
    before = counter(EMAIL_FAILED, template="receipt")
    for _ in range(8):
        await env.run_jobs()
        env.clock.advance(minutes=5)
    row = (await env.sql("SELECT status, attempts FROM billing.jobs WHERE kind = :k", k=EMAIL_JOB))[
        0
    ]
    assert (row[0], row[1]) == ("dead", 5) and env.mailbox.sent == []
    assert counter(EMAIL_FAILED, template="receipt") - before == 5


# --- payment_failed, suspended, cancelled -----------------------------------------------------


async def test_a_failed_charge_emails_once_with_the_grace_end_and_a_pay_link(env: Env) -> None:
    options = await env.activate()
    await env.run_jobs()
    env.clock.set(DEC1)
    hook = env.gateway.fail(options.gateway_subscription_id)
    await env.deliver(hook)
    await settle(env)
    (mail,) = env.mailbox.of("payment_failed")
    assert "4 Dec 2026" in mail.text  # grace_until, in IST (3 days after Dec 1)
    assert "https://rzp.io/i/" in mail.text and "/settings/billing" in mail.text
    assert "₹499.00" in mail.text and "auto" not in mail.text.lower()

    await env.deliver(hook.again())  # Razorpay redelivers
    await settle(env)
    assert len(env.mailbox.of("payment_failed")) == 1


async def test_grace_expiry_emails_the_suspension_once(env: Env) -> None:
    options = await env.activate()
    env.clock.set(DEC1)
    await env.deliver(env.gateway.fail(options.gateway_subscription_id))
    await settle(env)
    env.clock.advance(days=3, minutes=1)
    await env.run_job("grace.expire")
    await env.run_job("grace.expire")
    await env.run_jobs()
    (mail,) = env.mailbox.of("subscription_suspended")
    assert "Free plan" in mail.text and "data is safe" in mail.text
    assert "https://rzp.io/i/" in mail.text


async def test_cancelling_emails_the_access_until_date_once(env: Env) -> None:
    await env.activate()
    await env.service.cancel(env.user)
    await env.run_jobs()
    (mail,) = env.mailbox.of("subscription_cancelled")
    assert "1 Dec 2026" in mail.text and "As you asked" in mail.text


async def test_a_mandate_cancelled_in_the_bank_app_emails_too(env: Env) -> None:
    options = await env.activate()
    await env.deliver(env.gateway.cancel_from_customer(options.gateway_subscription_id))
    await settle(env)
    (mail,) = env.mailbox.of("subscription_cancelled")
    assert "bank or UPI app" in mail.text


# --- renewal_upcoming, refund_issued, credits_expiring ----------------------------------------


async def test_the_renewal_reminder_queues_one_email_per_period(env: Env) -> None:
    await env.activate()
    env.clock.set(DEC1 - timedelta(hours=30))
    await env.run_job("renewal.remind")
    await env.run_job("renewal.remind")
    await env.run_jobs()
    (mail,) = env.mailbox.of("renewal_upcoming")
    assert "1 Dec 2026" in mail.subject and "₹499.00" in mail.text


async def test_a_processed_refund_emails_the_credit_note(env: Env) -> None:
    await env.activate()
    await env.run_jobs()
    payment = UUID(str(await env.scalar("SELECT id FROM billing.payments")))
    await env.admin.refund(STAFF, payment, Paise(10000), "goodwill")
    await env.run_jobs()  # refund.issue, then the credit note render
    assert env.mailbox.of("refund_issued") == []  # not yet processed by the provider
    refund = next(iter(env.gateway.refunds.values()))
    hook = env.gateway.refund_processed(refund)
    await env.deliver(hook)
    await settle(env)
    (mail,) = env.mailbox.of("refund_issued")
    assert "₹100.00" in mail.text and "5-7 working days" in mail.text
    ((filename, _, data),) = mail.attachments
    assert filename.startswith("CN-2627-") and data.startswith(b"%PDF")
    await env.deliver(hook.again())
    await settle(env)
    assert len(env.mailbox.of("refund_issued")) == 1


async def test_expiring_credits_email_once(env: Env) -> None:
    from tests.integration.test_wallet import services, top_up

    _, _, wallet = services(env)
    await top_up(env, wallet)
    env.clock.set(env.clock.now() + timedelta(days=345))
    await env.run_job("credits.expire")
    await env.run_job("credits.expire")
    await env.run_jobs()
    (mail,) = env.mailbox.of("credits_expiring")
    assert "₹199.00" in mail.text


# --- the rules every email follows -----------------------------------------------------------


async def test_enqueueing_rolls_back_with_its_transaction(env: Env) -> None:
    await env.activate()
    from billing.domain.catalog import OwnerType

    async with env.uow() as uow:
        account = await uow.accounts.get_by_owner(OwnerType.USER, env.user)
    assert account is not None
    with pytest.raises(RuntimeError):
        async with env.uow() as uow:
            assert await enqueue_email(
                uow, env.bctx.config, "refund_issued", "r1", account,
                {"amount_paise": 1, "credit_note_id": None, "credit_note_number": None},
                env.clock.now(),
            )  # fmt: skip
            raise RuntimeError("the triggering change failed")
    assert await jobs_of(env, "refund_issued:") == 0
    async with env.uow() as uow:  # committed once, a duplicate enqueue is a no-op
        args = (uow, env.bctx.config, "refund_issued", "r1", account)
        data = {"amount_paise": 1, "credit_note_id": None, "credit_note_number": None}
        assert await enqueue_email(*args, data, env.clock.now())
        assert not await enqueue_email(*args, data, env.clock.now())
    assert await jobs_of(env, "refund_issued:") == 1


async def test_no_recipient_means_no_email_and_a_metric(env: Env) -> None:
    from billing.domain.catalog import OwnerType

    async with env.uow() as uow:
        account = await uow.accounts.get_or_create(OwnerType.USER, uuid4())  # no email
    assert account.billing_email is None
    before = counter(EMAIL_SKIPPED, template="receipt", reason="no_recipient")
    async with env.uow() as uow:
        assert not await enqueue_email(
            uow, env.bctx.config, "receipt", "x", account, {}, env.clock.now()
        )
    assert counter(EMAIL_SKIPPED, template="receipt", reason="no_recipient") - before == 1
    assert await jobs_of(env, "receipt:x") == 0


async def test_email_enabled_false_queues_nothing(env: Env) -> None:
    env.with_config(replace(env.bctx.config, email_enabled=False))
    await env.activate()
    await env.run_jobs()
    assert await env.scalar("SELECT count(*) FROM billing.jobs WHERE kind = :k", k=EMAIL_JOB) == 0
    assert env.mailbox.sent == []


async def test_old_invoices_rendered_by_a_backfill_are_not_announced(env: Env) -> None:
    await env.activate()
    env.clock.set(datetime(2027, 3, 1, tzinfo=UTC))  # the invoice is months old
    await env.run_jobs()
    assert env.mailbox.sent == []


async def test_simulated_emails_say_so_and_use_the_si_pdf(sim_lifecycle_env: Env) -> None:
    env = sim_lifecycle_env
    await env.activate()
    await env.run_jobs()
    (mail,) = env.mailbox.of("receipt")
    assert mail.subject.startswith("[SIMULATED] ")
    assert mail.text.startswith("This is a simulated payment. No money moved.")
    assert mail.attachments[0][0] == "SI-2627-000001.pdf"
    assert "SIMULATED" in mail.html


def test_the_api_process_never_builds_an_email_sender() -> None:
    from billing import main_api

    assert "make_email_sender" not in inspect.getsource(main_api)
