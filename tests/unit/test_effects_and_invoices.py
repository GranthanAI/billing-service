"""Effect executor and record_charge/GST, against in-memory stand-ins for the repositories."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from billing.adapters.ids import Uuid7Generator
from billing.app.charges import charge_description, record_charge
from billing.app.config import BillingConfig
from billing.app.effects import EffectContext, run_effects
from billing.app.events import PaymentFailedEvent, SubscriptionChangedEvent
from billing.app.ports import GatewayPayment
from billing.app.records import BillingAccount, PlanVersionRecord, SubscriptionRecord
from billing.domain.catalog import Interval, OwnerType
from billing.domain.money import Paise
from billing.domain.subscription import (
    CancelMandate,
    EmitPaymentFailed,
    EmitSubscriptionChanged,
    IssueRefund,
    SubscriptionState,
    SubStatus,
)
from tests.support import plans

NOW = datetime(2026, 11, 1, 4, 45, tzinfo=UTC)
END = datetime(2026, 12, 1, 4, 45, tzinfo=UTC)
USER = uuid4()
ACCOUNT_ID = uuid4()


def account(state: str | None = "10", **kw: Any) -> BillingAccount:
    return BillingAccount(ACCOUNT_ID, OwnerType.USER, USER, "u@example.com", state_code=state, **kw)


def record(plan: Any, status: SubStatus = SubStatus.ACTIVE) -> SubscriptionRecord:
    return SubscriptionRecord(
        uuid4(), ACCOUNT_ID, SubscriptionState(status, plan.id, Interval.MONTH, NOW, END)
    )


@dataclass
class FakeUow:
    """Just the repositories the code under test touches."""

    acct: BillingAccount | None = None
    published: list[object] = field(default_factory=list)
    queued: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)
    posted: list[Any] = field(default_factory=list)
    invoices_: list[Any] = field(default_factory=list)
    payments_: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        by_id = {p.id: PlanVersionRecord(p, NOW, "x") for p in (plans.FREE, plans.PRO, plans.MAX)}

        async def by_plan_id(i: UUID) -> PlanVersionRecord | None:
            return by_id.get(i)

        async def enqueue(e: object) -> None:
            self.published.append(e)

        async def job(
            kind: str, payload: dict[str, Any], run_at: datetime, key: str | None
        ) -> bool:
            self.queued.append((kind, payload, key))
            return True

        async def insert_payment(p: Any) -> bool:
            if p.gateway_payment_id in self.payments_:
                return False
            self.payments_[p.gateway_payment_id] = p
            return True

        async def get_account(_: UUID) -> BillingAccount | None:
            return self.acct

        async def post(tx: Any, ref_type: str, ref_id: str, at: datetime) -> bool:
            self.posted.append(tx)
            return True

        counters: dict[str, int] = {}

        async def allocate(series: str, fy: str) -> str:
            counters[series + fy] = counters.get(series + fy, 0) + 1
            return f"{series}/{fy}/{counters[series + fy]:06d}"

        async def insert_invoice(inv: Any, lines: Any) -> None:
            self.invoices_.append((inv, list(lines)))

        self.plans = SimpleNamespace(by_id=by_plan_id)
        self.publisher = SimpleNamespace(enqueue=enqueue)
        self.jobs = SimpleNamespace(enqueue=job)
        self.payments = SimpleNamespace(insert=insert_payment)
        self.accounts = SimpleNamespace(get=get_account)
        self.ledger = SimpleNamespace(post=post)
        self.invoices = SimpleNamespace(allocate_number=allocate, insert=insert_invoice)


def payment(amount: int = 49900, pid: str = "pay_1", at: datetime = NOW) -> GatewayPayment:
    return GatewayPayment(
        pid, Paise(amount), "INR", "captured", "card", Paise(998), Paise(180), None, None, None, at, {}, {"id": pid}  # noqa: E501
    )  # fmt: skip


# --- effect executor -----------------------------------------------------------------------


async def test_changed_event_carries_plans_and_statuses() -> None:
    uow = FakeUow(acct=account())
    before = record(plans.PRO, SubStatus.INCOMPLETE)
    after = SubscriptionRecord(
        before.id,
        ACCOUNT_ID,
        SubscriptionState(SubStatus.ACTIVE, plans.MAX.id, Interval.MONTH, NOW, END),
    )
    await run_effects(
        uow, [EmitSubscriptionChanged()], before, after, account(), EffectContext(), NOW
    )  # type: ignore[arg-type]
    (event,) = uow.published
    assert isinstance(event, SubscriptionChangedEvent)
    assert (event.plan_code, event.previous_plan_code) == ("max", "pro")
    assert (event.status, event.previous_status) == (SubStatus.ACTIVE, SubStatus.INCOMPLETE)
    assert (event.owner_id, event.billing_account_id, event.current_period_end) == (
        USER,
        ACCOUNT_ID,
        END,
    )


async def test_payment_failed_uses_the_failed_payment_when_there_is_one() -> None:
    uow = FakeUow(acct=account())
    rec = record(plans.PRO)
    ctx = EffectContext(failed_payment=payment(49900, "pay_bad"), next_retry_at=END)
    await run_effects(uow, [EmitPaymentFailed()], rec, rec, account(), ctx, NOW)  # type: ignore[arg-type]
    (event,) = uow.published
    assert isinstance(event, PaymentFailedEvent)
    assert (event.gateway_payment_id, event.amount, event.next_retry_at) == ("pay_bad", 49900, END)


async def test_payment_failed_without_a_payment_falls_back_to_the_plan_price() -> None:
    uow = FakeUow(acct=account())
    rec = record(plans.MAX)
    await run_effects(uow, [EmitPaymentFailed()], rec, rec, account(), EffectContext(), NOW)  # type: ignore[arg-type]
    (event,) = uow.published
    assert isinstance(event, PaymentFailedEvent)
    assert (event.gateway_payment_id, event.amount) == ("", 149900)


async def test_cancel_mandate_becomes_a_deduplicated_gateway_cancel_job() -> None:
    uow = FakeUow(acct=account())
    rec = record(plans.PRO)
    await run_effects(
        uow,
        [CancelMandate("sub_1", True), CancelMandate("sub_2", False)],
        rec,
        rec,
        account(),
        EffectContext(),
        NOW,
    )  # type: ignore[arg-type]
    assert uow.queued == [
        (
            "gateway.cancel",
            {"gateway_subscription_id": "sub_1", "at_cycle_end": True},
            "gateway.cancel:sub_1:1",
        ),
        (
            "gateway.cancel",
            {"gateway_subscription_id": "sub_2", "at_cycle_end": False},
            "gateway.cancel:sub_2:0",
        ),
    ]


async def test_issue_refund_becomes_a_refund_job_with_the_old_interval() -> None:
    uow = FakeUow(acct=account())
    before = record(plans.PRO)
    pid = uuid4()
    ctx = EffectContext(refund_payment_id=pid, refund_key="upgrade:abc")
    await run_effects(
        uow, [IssueRefund(Paise(24950), "upgrade")], before, before, account(), ctx, NOW
    )  # type: ignore[arg-type]
    ((kind, payload, key),) = uow.queued
    assert kind == "refund.issue" and key == "refund.issue:upgrade:abc"
    assert payload == {
        "subscription_id": str(before.id), "payment_id": str(pid), "amount": 24950,
        "reason": "upgrade", "refund_key": "upgrade:abc", "interval": "month",
    }  # fmt: skip


async def test_issue_refund_without_a_payment_is_skipped() -> None:
    uow = FakeUow(acct=account())
    rec = record(plans.PRO)
    await run_effects(
        uow, [IssueRefund(Paise(100), "upgrade")], rec, rec, account(), EffectContext(), NOW
    )  # type: ignore[arg-type]
    assert uow.queued == []


# --- GST and the invoice line --------------------------------------------------------------


def cfg(**kw: Any) -> BillingConfig:
    return BillingConfig(razorpay_key_id="k", supplier_state_code="10", **kw)


async def charge(
    uow: FakeUow, config: BillingConfig, interval: Interval = Interval.MONTH, seats: int = 1
) -> bool:
    return await record_charge(
        uow, record(plans.PRO), payment(), NOW, END, interval, seats,  # type: ignore[arg-type]
        plan_code="pro", config=config, ids=Uuid7Generator(),
    )  # fmt: skip


async def test_same_state_customer_gets_cgst_and_sgst() -> None:
    uow = FakeUow(acct=account("10"))
    assert await charge(uow, cfg())
    ((inv, lines),) = uow.invoices_
    assert (inv.number, inv.kind, inv.status) == ("GA/2627/000001", "tax_invoice", "paid")
    assert (inv.taxable_paise, inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (
        42288,
        3806,
        3806,
        0,
    )
    assert (inv.customer_state_code, inv.supplier_state_code) == ("10", "10")
    (line,) = lines
    assert line.description == "GranthanAI Pro · monthly · 1 Nov 2026 – 1 Dec 2026"
    assert (line.sac_code, line.quantity, line.unit_price_paise, line.taxable_paise) == (
        "998319",
        1,
        42288,
        42288,
    )


async def test_other_state_customer_gets_igst() -> None:
    uow = FakeUow(acct=account("29"))
    await charge(uow, cfg())
    ((inv, _),) = uow.invoices_
    assert (inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (0, 0, 7612)
    assert inv.total_paise == 49900


async def test_unregistered_supplier_issues_a_bill_of_supply_without_tax() -> None:
    uow = FakeUow(acct=account("29"))
    await charge(uow, cfg(gst_registered=False))
    ((inv, _),) = uow.invoices_
    assert (inv.number, inv.kind) == ("BS/2627/000001", "bill_of_supply")
    assert (inv.taxable_paise, inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (49900, 0, 0, 0)
    assert all(p.account_code != "liability:gst_output:igst" for p in uow.posted[0].postings)


async def test_ledger_posting_is_balanced_and_keyed_by_the_payment() -> None:
    uow = FakeUow(acct=account())
    await charge(uow, cfg())
    (tx,) = uow.posted
    assert tx.idempotency_key == "charge:pay_1"
    assert sum(p.amount for p in tx.postings) == 0


async def test_yearly_charge_goes_to_deferred_revenue() -> None:
    uow = FakeUow(acct=account())
    await charge(uow, cfg(), Interval.YEAR)
    codes = {p.account_code for p in uow.posted[0].postings}
    assert "liability:deferred_revenue" in codes and "revenue:subscription" not in codes


async def test_a_payment_seen_before_writes_nothing_more() -> None:
    uow = FakeUow(acct=account())
    assert await charge(uow, cfg()) is True
    assert await charge(uow, cfg()) is False
    assert len(uow.invoices_) == 1 and len(uow.posted) == 1


async def test_seats_set_the_quantity_and_unit_price() -> None:
    uow = FakeUow(acct=account())
    await charge(uow, cfg(), seats=2)
    (line,) = uow.invoices_[0][1]
    assert (line.quantity, line.unit_price_paise) == (2, 21144)


async def test_an_account_without_a_state_cannot_be_invoiced() -> None:
    with pytest.raises(ValueError, match="state code"):
        await charge(FakeUow(acct=account(None)), cfg())


async def test_an_uncaptured_payment_is_refused() -> None:
    uow = FakeUow(acct=account())
    bad = payment()
    failed = GatewayPayment(
        bad.id, bad.amount, "INR", "failed", None, None, None, None, None, None, NOW, {}, {}
    )
    with pytest.raises(ValueError, match="not captured"):
        await record_charge(
            uow, record(plans.PRO), failed, NOW, END, Interval.MONTH,  # type: ignore[arg-type]
            plan_code="pro", config=cfg(), ids=Uuid7Generator(),
        )  # fmt: skip


def test_description_uses_ist_dates() -> None:
    start = datetime(2027, 3, 31, 20, 0, tzinfo=UTC)  # 1 April 01:30 IST
    text = charge_description(
        "GranthanAI", "max", Interval.YEAR, start, datetime(2028, 3, 31, 20, 0, tzinfo=UTC)
    )
    assert text == "GranthanAI Max · yearly · 1 Apr 2027 – 1 Apr 2028"


async def test_the_fee_is_stored_without_the_gst_it_includes() -> None:
    uow = FakeUow(acct=account())
    await charge(uow, cfg())  # the helper payment: fee 998 including 180 of GST
    stored = uow.payments_["pay_1"]
    assert (stored.fee_paise, stored.fee_gst_paise) == (818, 180)
    assert stored.fee_paise + stored.fee_gst_paise == 998  # what Razorpay deducts
