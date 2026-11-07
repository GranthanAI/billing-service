"""Stage G: organisation wallet top-ups by payment link or bank transfer."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

from billing.app.org_topups import OrgTopups
from billing.domain.catalog import OrgVariant
from billing.domain.ledger import wallet_code
from tests.integration.billing_env import Env
from tests.integration.test_orgs import balance, make_org, pay, staff, unbalanced

AMOUNT = 500_000  # Rs 5,000, GST included: the minimum
TAXABLE = 423_729  # round(500000 / 1.18)
CGST, SGST = 38_135, 38_136  # tax 76,271 in the same state (Bihar): CGST rounds down
ADMIN = "/admin/billing/v1/orgs"


async def org_account(env: Env, org_id: UUID) -> UUID:
    return UUID(
        str(
            await env.scalar(
                "SELECT id FROM billing.billing_accounts "
                "WHERE owner_type = 'org' AND owner_id = :o",
                o=org_id,
            )
        )
    )


async def create(env: Env, org_id: UUID, amount: int, reason: str = "q3 credits") -> Any:
    async with env.api() as http:
        return await http.post(
            f"{ADMIN}/{org_id}/wallet/topups",
            json={"amount_paise": amount, "reason": reason},
            headers=staff(),
        )


async def post(env: Env, path: str, body: dict[str, Any]) -> Any:
    async with env.api() as http:
        return await http.post(f"{ADMIN}/{path}", json=body, headers=staff())


def link_id_of(env: Env) -> str:
    (link_id,) = [k for k in env.gateway.payment_links]
    return link_id


async def one_org(env: Env) -> Any:
    org = make_org(env)
    issued = await env.org_billing.subscribe(
        org, "org_pro", OrgVariant.COMPANY, 5,
        start=None, with_link=False, actor=uuid4(), reason="pilot",
    )  # fmt: skip
    await pay(env, issued.invoice_id)  # an org with an account, paid by bank
    return org


async def test_below_the_minimum_or_not_whole_rupees_is_422(env: Env) -> None:
    org = await one_org(env)
    below = await create(env, org.org_id, AMOUNT - 100)
    assert below.status_code == 422 and below.json()["code"] == "VALIDATION_FAILED"
    paise = await create(env, org.org_id, AMOUNT + 50)
    assert paise.status_code == 422 and "whole number of rupees" in paise.json()["detail"]
    no_reason = await create(env, org.org_id, AMOUNT, reason="")
    assert no_reason.status_code == 422
    assert await env.scalar("SELECT count(*) FROM billing.wallet_topup_requests") == 0
    assert env.gateway.payment_links == {}


async def test_link_paid_credits_the_wallet_and_issues_a_b2b_invoice(env: Env) -> None:
    org = await one_org(env)
    acct = await org_account(env, org.org_id)
    resp = await create(env, org.org_id, AMOUNT)
    assert resp.status_code == 201
    body = resp.json()
    assert body["amount_paise"] == AMOUNT and body["credit_taxable_paise"] == TAXABLE
    assert body["status"] == "pending" and body["payment_link_url"].startswith("https://rzp.io/")
    link_id = link_id_of(env)
    assert env.gateway.payment_link_notes[link_id] == {
        "kind": "org_topup",
        "request_id": body["request_id"],
    }
    assert env.gateway.payment_links[link_id].amount == AMOUNT

    await env.deliver(env.gateway.pay_link(link_id))
    await env.process()

    request = (
        await env.sql(
            "SELECT status, payment_id, gateway_link_id FROM billing.wallet_topup_requests"
        )
    )[0]
    assert request.status == "paid" and request.gateway_link_id == link_id
    payment = (
        await env.sql(
            "SELECT id, purpose, amount_paise, status, method FROM billing.payments "
            "WHERE purpose = 'topup'"
        )
    )[0]
    assert (payment.amount_paise, payment.status) == (AMOUNT, "captured")
    assert payment.id == request.payment_id
    lot = (
        await env.sql(
            "SELECT amount_paise, remaining_paise, expires_at - :t AS validity "
            "FROM billing.credit_lots",
            t=env.clock.now(),
        )
    )[0]
    assert (lot.amount_paise, lot.remaining_paise) == (TAXABLE, TAXABLE)
    assert 364 <= lot.validity.days <= 366  # twelve months
    assert await balance(env, wallet_code(acct)) == -TAXABLE
    assert await balance(env, "asset:razorpay_clearing") == AMOUNT
    invoice = (
        await env.sql(
            "SELECT kind, status, customer_gstin, customer_state_code, taxable_paise, cgst_paise, "
            "sgst_paise, igst_paise, total_paise FROM billing.invoices WHERE payment_id = :p",
            p=payment.id,
        )
    )[0]
    assert tuple(invoice) == (
        "tax_invoice", "paid", "10BBBBB1111B1Z5", "10", TAXABLE, CGST, SGST, 0, AMOUNT,
    )  # fmt: skip
    assert (await env.counters.wallet_get(acct))["balance"] == TAXABLE
    assert await unbalanced(env) == 0


async def test_mark_paid_by_bank_transfer_is_idempotent_and_closes_the_link(env: Env) -> None:
    org = await one_org(env)
    acct = await org_account(env, org.org_id)
    request_id = (await create(env, org.org_id, AMOUNT)).json()["request_id"]
    bank_before = await balance(env, "asset:bank")

    first = await post(
        env, f"{org.org_id}/wallet/topups/{request_id}/mark-paid",
        {"reference": "UTR-77", "reason": "NEFT received"},
    )  # fmt: skip
    assert first.status_code == 200
    assert first.json() == {"request_id": request_id, "status": "paid", "changed": True}
    again = await post(
        env, f"{org.org_id}/wallet/topups/{request_id}/mark-paid",
        {"reference": "UTR-77", "reason": "NEFT received"},
    )  # fmt: skip
    assert again.status_code == 200 and again.json()["changed"] is False

    payment = await env.sql(
        "SELECT gateway_payment_id, method, amount_paise FROM billing.payments "
        "WHERE purpose = 'topup'"
    )
    assert [(p.gateway_payment_id, p.method, p.amount_paise) for p in payment] == [
        ("bank:UTR-77", "bank_transfer", AMOUNT)
    ]
    assert await env.scalar("SELECT count(*) FROM billing.credit_lots") == 1
    assert await balance(env, wallet_code(acct)) == -TAXABLE
    assert await balance(env, "asset:bank") - bank_before == AMOUNT
    assert env.gateway.payment_links[link_id_of(env)].status == "cancelled"
    assert (await env.counters.wallet_get(acct))["balance"] == TAXABLE
    assert (
        await env.scalar(
            "SELECT count(*) FROM billing.invoices WHERE kind = 'tax_invoice' AND total_paise = :a",
            a=AMOUNT,
        )
        == 1
    )
    assert await unbalanced(env) == 0
    audited = await env.sql(
        "SELECT action, reason FROM billing.admin_audit WHERE action LIKE 'org.topup%' ORDER BY id"
    )
    assert [(a.action, a.reason) for a in audited] == [
        ("org.topup_create", "q3 credits"),
        ("org.topup_mark_paid", "NEFT received"),
    ]


async def test_webhook_and_mark_paid_racing_credit_once(env: Env) -> None:
    org = await one_org(env)
    acct = await org_account(env, org.org_id)
    request_id = UUID((await create(env, org.org_id, AMOUNT)).json()["request_id"])
    await env.deliver(env.gateway.pay_link(link_id_of(env)))
    svc = OrgTopups(env.bctx)

    async def bank(ref: str) -> dict[str, Any]:
        return await svc.mark_paid(org.org_id, request_id, reference=ref, actor=uuid4(), reason="r")

    results = await asyncio.gather(env.process(), bank("UTR-A"), bank("UTR-B"))
    assert results[0] == 1
    assert sum(1 for r in results[1:] if r["changed"]) <= 1  # at most one of them won the lock

    assert await env.scalar("SELECT count(*) FROM billing.payments WHERE purpose = 'topup'") == 1
    assert await env.scalar("SELECT count(*) FROM billing.credit_lots") == 1
    assert await balance(env, wallet_code(acct)) == -TAXABLE
    assert (await env.counters.wallet_get(acct))["balance"] == TAXABLE
    assert (
        await env.scalar("SELECT count(*) FROM billing.invoices WHERE total_paise = :a", a=AMOUNT)
        == 1
    )
    assert await unbalanced(env) == 0


async def test_cancel_pending_closes_the_link_and_cancel_after_paid_is_409(env: Env) -> None:
    org = await one_org(env)
    cancelled_id = (await create(env, org.org_id, AMOUNT)).json()["request_id"]
    link = link_id_of(env)
    done = await post(env, f"{org.org_id}/wallet/topups/{cancelled_id}/cancel", {"reason": "oops"})
    assert done.status_code == 200 and done.json()["status"] == "cancelled"
    assert env.gateway.payment_links[link].status == "cancelled"
    late = await post(
        env, f"{org.org_id}/wallet/topups/{cancelled_id}/mark-paid",
        {"reference": "UTR-1", "reason": "too late"},
    )  # fmt: skip
    assert late.status_code == 409 and late.json()["code"] == "SUBSCRIPTION_STATE"

    paid_id = (await create(env, org.org_id, AMOUNT + 100_000)).json()["request_id"]
    marked = await post(
        env,
        f"{org.org_id}/wallet/topups/{paid_id}/mark-paid",
        {"reference": "UTR-2", "reason": "bank ok"},
    )
    assert marked.status_code == 200, marked.text
    refused = await post(env, f"{org.org_id}/wallet/topups/{paid_id}/cancel", {"reason": "nope"})
    assert refused.status_code == 409 and refused.json()["code"] == "SUBSCRIPTION_STATE"
    assert await env.scalar("SELECT count(*) FROM billing.credit_lots") == 1


async def test_routes_are_404_when_pay_as_you_go_is_off(env: Env) -> None:
    org = await one_org(env)
    request_id = (await create(env, org.org_id, AMOUNT)).json()["request_id"]
    env.set_payg(False)
    paths = [
        (f"{org.org_id}/wallet/topups", {"amount_paise": AMOUNT, "reason": "x1x"}),
        (f"{org.org_id}/wallet/topups/{request_id}/mark-paid", {"reference": "r", "reason": "x1x"}),
        (f"{org.org_id}/wallet/topups/{request_id}/cancel", {"reason": "x1x"}),
    ]
    for path, body in paths:
        resp = await post(env, path, body)
        assert resp.status_code == 404, path
    assert (
        await env.scalar("SELECT status FROM billing.wallet_topup_requests") == "pending"
    )  # nothing changed


async def test_only_billing_admin_may_write(env: Env) -> None:
    org = await one_org(env)
    async with env.api() as http:
        support = await http.post(
            f"{ADMIN}/{org.org_id}/wallet/topups",
            json={"amount_paise": AMOUNT, "reason": "abc"},
            headers={**staff(), "X-Staff-Role": "support"},
        )
        unknown = await http.post(
            f"{ADMIN}/{uuid4()}/wallet/topups",
            json={"amount_paise": AMOUNT, "reason": "abc"},
            headers=staff(),
        )
    assert support.status_code == 403
    assert unknown.status_code == 404
