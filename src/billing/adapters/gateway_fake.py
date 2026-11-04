"""In-memory PaymentGateway for local runs (PAYMENT_GATEWAY=fake) and as the test fake's base.

It keeps plans, subscriptions, payments and refunds; it never sends a webhook by itself. The
test fake in tests/support/fake_gateway.py adds the helpers that move this state forward and
build the matching signed webhook bodies.
"""

import calendar
import itertools
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from billing.adapters.clock import SystemClock
from billing.adapters.razorpay import signatures
from billing.app.errors import GatewayRejected
from billing.app.ports import (
    Clock,
    GatewayPayment,
    GatewayPlanSpec,
    GatewayRefund,
    GatewaySubscription,
)
from billing.domain.money import Paise


def add_months(dt: datetime, months: int) -> datetime:
    index = dt.month - 1 + months
    year, month = dt.year + index // 12, index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


class InMemoryGateway:
    def __init__(
        self,
        *,
        key_secret: str = "fake_key_secret",
        webhook_secret: str = "fake_webhook_secret",
        clock: Clock | None = None,
    ) -> None:
        self.key_secret = key_secret
        self.webhook_secret = webhook_secret
        self.clock: Clock = clock or SystemClock()
        self.plans: dict[str, GatewayPlanSpec] = {}
        self.subscriptions: dict[str, GatewaySubscription] = {}
        self.payments: dict[str, GatewayPayment] = {}
        self.refunds: dict[str, GatewayRefund] = {}
        self.cycle_end_cancels: set[str] = set()
        self._plan_seq = itertools.count(1)
        self._sub_seq = itertools.count(1)
        self._pay_seq = itertools.count(1)
        self._refund_seq = itertools.count(1)

    # --- plans ---------------------------------------------------------------------------

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        checksum, interval = spec.notes.get("checksum"), spec.notes.get("interval")
        for plan_id, existing in self.plans.items():
            if (
                existing.notes.get("checksum") == checksum
                and existing.notes.get("interval") == interval
            ):
                return plan_id
        plan_id = f"plan_fake_{next(self._plan_seq):04d}"
        self.plans[plan_id] = spec
        return plan_id

    def period_months(self, plan_id: str) -> int:
        spec = self.plans.get(plan_id)
        return 12 if spec is not None and spec.period == "yearly" else 1

    def period_end(self, plan_id: str, start: datetime) -> datetime:
        """End of the billing period that starts at `start`. Besides Razorpay's `monthly` and
        `yearly`, a plan may use the fake-only period `"<n>days"` so a test can run a whole
        subscription lifecycle in a few weeks."""
        spec = self.plans.get(plan_id)
        if spec is not None and spec.period.endswith("days"):
            return start + timedelta(days=int(spec.period.removesuffix("days")))
        return add_months(start, self.period_months(plan_id))

    # --- subscriptions -------------------------------------------------------------------

    async def create_subscription(
        self,
        plan_id: str,
        *,
        total_count: int,
        start_at: datetime | None,
        notes: Mapping[str, str],
        lookup_first: bool = False,
    ) -> GatewaySubscription:
        ref = notes.get("billing_ref")
        if ref:
            for existing in self.subscriptions.values():
                if existing.plan_id == plan_id and existing.notes.get("billing_ref") == ref:
                    return existing
        sub_id = f"sub_fake_{next(self._sub_seq):06d}"
        sub = GatewaySubscription(
            id=sub_id,
            plan_id=plan_id,
            status="created",
            current_start=None,
            current_end=None,
            charge_at=start_at,
            start_at=start_at,
            paid_count=0,
            notes=dict(notes),
            short_url=f"https://rzp.io/i/{sub_id}",
        )
        self.subscriptions[sub_id] = sub
        return sub

    def _sub(self, subscription_id: str) -> GatewaySubscription:
        try:
            return self.subscriptions[subscription_id]
        except KeyError:
            raise GatewayRejected("BAD_REQUEST_ERROR", "The id provided does not exist") from None

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        return self._sub(subscription_id)

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> GatewaySubscription:
        sub = self._sub(subscription_id)
        if sub.status in ("cancelled", "completed", "expired"):
            raise GatewayRejected(
                "BAD_REQUEST_ERROR",
                f"Subscription is not cancellable in {sub.status} status",
            )
        if at_cycle_end:
            self.cycle_end_cancels.add(subscription_id)
            return sub
        sub = replace(sub, status="cancelled", charge_at=None)
        self.subscriptions[subscription_id] = sub
        return sub

    # --- payments and refunds ------------------------------------------------------------

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        try:
            return self.payments[payment_id]
        except KeyError:
            raise GatewayRejected("BAD_REQUEST_ERROR", "The id provided does not exist") from None

    async def list_subscription_payments(self, subscription_id: str) -> list[GatewayPayment]:
        found = [
            p
            for p in self.payments.values()
            if p.subscription_id == subscription_id and p.status == "captured"
        ]
        return sorted(found, key=lambda p: p.created_at, reverse=True)

    async def refund_payment(
        self, payment_id: str, amount: Paise, *, notes: Mapping[str, str]
    ) -> GatewayRefund:
        pay = await self.fetch_payment(payment_id)
        key = notes.get("refund_key")
        if key:
            for existing in self.refunds.values():
                if existing.payment_id == payment_id and existing.notes.get("refund_key") == key:
                    return existing
        if amount > pay.amount:
            raise GatewayRejected(
                "BAD_REQUEST_ERROR", "The refund amount provided is greater than amount captured"
            )
        refund = GatewayRefund(
            id=f"rfnd_fake_{next(self._refund_seq):06d}",
            payment_id=payment_id,
            amount=amount,
            status="processed",
            notes=dict(notes),
        )
        self.refunds[refund.id] = refund
        return refund

    async def fetch_refund(self, refund_id: str) -> GatewayRefund:
        try:
            return self.refunds[refund_id]
        except KeyError:
            raise GatewayRejected("BAD_REQUEST_ERROR", "The id provided does not exist") from None

    # --- signatures ----------------------------------------------------------------------

    def verify_checkout_signature(
        self, payment_id: str, subscription_id: str, signature: str
    ) -> bool:
        return signatures.verify_checkout(self.key_secret, payment_id, subscription_id, signature)

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        return signatures.verify_webhook(self.webhook_secret, raw_body, signature)

    def next_payment_id(self) -> str:
        return f"pay_fake_{next(self._pay_seq):06d}"
