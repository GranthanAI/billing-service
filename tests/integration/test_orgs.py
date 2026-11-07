"""Stage F: organisation subscriptions by invoice, org grants, seats, renewal and the admin API."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from billing.app.errors import ChangePending, SubscriptionStateError, ValidationFailed
from billing.app.records import OrgInfo
from billing.domain.catalog import OrgVariant
from tests.integration.billing_env import ORG_TOKEN, Env


def staff() -> dict[str, str]:
    return {
        "X-User-Id": str(uuid4()),
        "X-Staff-Role": "billing_admin",
        "Idempotency-Key": str(uuid4()),
    }


SEAT = 838800  # org_pro company, per seat per year, GST included


def make_org(env: Env, *, gstin: str | None = "10BBBBB1111B1Z5", state: str = "10") -> OrgInfo:
    org = OrgInfo(uuid4(), "Acme Pvt Ltd", OrgVariant.COMPANY, gstin, "ap@acme.test", state, None)
    env.orgs.add_org(org)
    return org


async def subscribe(env: Env, org: OrgInfo, seats: int = 5, plan: str = "org_pro") -> Any:
    return await env.org_billing.subscribe(
        org, plan, OrgVariant.COMPANY, seats,
        start=None, with_link=True, actor=uuid4(), reason="pilot",
    )  # fmt: skip


async def pay(env: Env, invoice_id: UUID, ref: str = "UTR1") -> bool:
    return await env.org_billing.mark_paid(
        invoice_id, reference=ref, paid_at=None, amount=None, actor=uuid4(), reason="bank"
    )


async def balance(env: Env, code: str) -> int:
    value = await env.scalar(
        "SELECT coalesce(sum(e.amount_paise), 0) FROM billing.ledger_entries e "
        "JOIN billing.ledger_accounts a ON a.id = e.account_id WHERE a.code = :c",
        c=code,
    )
    return int(value)  # type: ignore[call-overload]


async def unbalanced(env: Env) -> int:
    return len(
        await env.sql(
            "SELECT transaction_id FROM billing.ledger_entries GROUP BY 1 HAVING sum(amount_paise) <> 0"  # noqa: E501
        )
    )


async def sub_row(env: Env, org: OrgInfo) -> Any:
    rows = await env.sql(
        "SELECT s.status::text, s.seats, s.pending_seats, s.current_period_end "
        "FROM billing.subscriptions s "
        "JOIN billing.billing_accounts a ON a.id = s.billing_account_id WHERE a.owner_id = :o",
        o=org.org_id,
    )
    return rows[0]


async def test_invoice_then_bank_transfer_activates_org_plan(env: Env) -> None:
    org, member = make_org(env), uuid4()
    env.orgs.join(member, org.org_id, OrgVariant.COMPANY)
    assert await env.plan_code(member) == "free"

    issued = await subscribe(env, org)
    assert issued.total_paise == 5 * SEAT
    assert issued.payment_link_url and issued.payment_link_url.startswith("https://rzp.io/")
    assert (await sub_row(env, org))[0] == "incomplete"
    inv = (await env.sql("SELECT status, igst_paise, cgst_paise FROM billing.invoices"))[0]
    assert inv.status == "issued" and inv.igst_paise == 0 and inv.cgst_paise > 0  # same state

    assert await pay(env, issued.invoice_id) is True
    assert await pay(env, issued.invoice_id) is False
    assert (await sub_row(env, org))[0] == "active"
    assert await env.plan_code(member) == "org_pro"
    assert await balance(env, "asset:bank") == 5 * SEAT
    assert await balance(env, "asset:razorpay_clearing") == 0
    assert await unbalanced(env) == 0
    assert await env.scalar("SELECT count(*) FROM billing.payments") == 1


async def test_payment_link_webhook_activates_and_replay_is_harmless(env: Env) -> None:
    org, member = make_org(env, state="29"), uuid4()  # another state: IGST
    env.orgs.join(member, org.org_id, OrgVariant.COMPANY)
    issued = await subscribe(env, org)
    link_id = next(iter(env.gateway.payment_links))
    hook = env.gateway.pay_link(link_id)
    await env.deliver(hook, hook.again())
    await env.process()

    assert (await sub_row(env, org))[0] == "active"
    assert await env.plan_code(member) == "org_pro"
    assert await balance(env, "asset:razorpay_clearing") == issued.total_paise
    inv = (await env.sql("SELECT status, igst_paise, cgst_paise FROM billing.invoices"))[0]
    assert inv.status == "paid" and inv.igst_paise > 0 and inv.cgst_paise == 0
    assert await env.scalar("SELECT count(*) FROM billing.payments") == 1
    assert await unbalanced(env) == 0


async def test_leaving_drops_the_entitlement_and_an_outage_degrades(env: Env) -> None:
    org, member = make_org(env), uuid4()
    env.orgs.join(member, org.org_id, OrgVariant.COMPANY)
    await pay(env, (await subscribe(env, org)).invoice_id)
    assert await env.plan_code(member) == "org_pro"
    env.orgs.leave(member)
    await env.entitlements.invalidate(member)
    assert await env.plan_code(member) == "free"

    env.orgs.join(member, org.org_id, OrgVariant.COMPANY)
    await env.entitlements.invalidate(member)
    env.orgs.down = True
    assert await env.plan_code(member) == "free"  # org-service is down: personal plan, no error


async def test_rules_for_subscribing(env: Env) -> None:
    org = make_org(env)
    with pytest.raises(ValidationFailed):  # below the company minimum of 5 seats
        await subscribe(env, org, seats=4)
    with pytest.raises(ValidationFailed):
        await subscribe(env, org, plan="pro")
    with pytest.raises(ValidationFailed):
        await subscribe(env, make_org(env, state=""))
    await subscribe(env, org)
    with pytest.raises(SubscriptionStateError):
        await subscribe(env, org)


async def test_more_seats_are_prorated_fewer_wait_for_renewal(env: Env) -> None:
    org = make_org(env)
    await pay(env, (await subscribe(env, org)).invoice_id)
    env.clock.advance(days=73)
    outcome, extra = await env.org_billing.change_seats(org.org_id, 7, actor=uuid4(), reason="up")
    assert outcome == "invoiced" and extra is not None
    assert 2 * SEAT * 290 // 365 <= extra.total_paise <= 2 * SEAT * 293 // 365 + 1
    with pytest.raises(ChangePending):
        await env.org_billing.change_seats(org.org_id, 8, actor=uuid4(), reason="again")
    await pay(env, extra.invoice_id, "UTR2")
    assert (await sub_row(env, org))[1] == 7
    assert await unbalanced(env) == 0

    outcome, none = await env.org_billing.change_seats(org.org_id, 6, actor=uuid4(), reason="cut")
    assert (outcome, none) == ("at_renewal", None)
    row = await sub_row(env, org)
    assert (row[1], row[2]) == (7, 6)


async def test_renewal_invoice_once_then_unpaid_goes_past_due(env: Env) -> None:
    org, member = make_org(env), uuid4()
    env.orgs.join(member, org.org_id, OrgVariant.COMPANY)
    await pay(env, (await subscribe(env, org)).invoice_id)
    assert await env.org_billing.renew() == (0, 0)  # too early
    env.clock.advance(days=340)  # inside the 30-day notice
    assert await env.org_billing.renew() == (1, 0)
    assert await env.org_billing.renew() == (0, 0)  # not twice
    assert await env.scalar("SELECT count(*) FROM billing.invoices WHERE status = 'issued'") == 1

    env.clock.advance(days=30)  # the period has ended and nothing was paid
    assert await env.org_billing.renew() == (0, 1)
    assert (await sub_row(env, org))[0] == "past_due"
    assert await env.plan_code(member) == "org_pro"  # still inside the grace

    renewal = await env.scalar("SELECT id FROM billing.invoices WHERE status = 'issued'")
    await pay(env, UUID(str(renewal)), "UTR3")
    assert (await sub_row(env, org))[0] == "active"


async def test_admin_api_subscribe_pay_and_usage_report(env: Env) -> None:
    org = make_org(env)
    async with env.api() as api:
        r = await api.post(
            f"/admin/billing/v1/orgs/{org.org_id}/subscriptions",
            headers=staff(),
            json={"plan_code": "org_pro", "variant": "company", "seats": 5, "reason": "pilot"},
        )
        assert r.status_code == 201, r.text
        invoice_id = r.json()["invoice_id"]
        pay_url = f"/admin/billing/v1/invoices/{invoice_id}/mark-paid"
        assert (
            await api.post(pay_url, headers=staff(), json={"reference": "U"})
        ).status_code == 422
        paid = await api.post(
            pay_url, headers=staff(), json={"reference": "UTR1", "reason": "bank statement 7 Nov"}
        )
        assert paid.status_code == 200 and paid.json()["changed"] is True
        url = f"/internal/orgs/{org.org_id}/usage?from=2026-11-01&to=2026-11-30"
        assert (await api.get(url)).status_code == 401
        ok = await api.get(url, headers={"Authorization": f"Bearer {ORG_TOKEN}"})
        assert ok.status_code == 200 and ok.json()["rows"] == []
    assert (await sub_row(env, org))[0] == "active"
    assert await env.scalar("SELECT count(*) FROM billing.admin_audit") == 2
