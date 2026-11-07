"""Recording fake PaymentGateway that also plays Razorpay: it moves its own state forward and
builds the signed webhook bodies Razorpay would send, from the sample payloads in
tests/fixtures/razorpay/."""

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

from billing.adapters.gateway_fake import InMemoryGateway
from billing.adapters.razorpay import mapping, signatures
from billing.app.ports import (
    GatewayPayment,
    GatewayPlanSpec,
    GatewayRefund,
    GatewaySubscription,
)
from billing.domain.money import Paise

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "razorpay"
_EVENT_SEQ = count(1)


@dataclass(frozen=True)
class Webhook:
    body: bytes
    signature: str
    event_id: str
    event_type: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Razorpay-Signature": self.signature, "X-Razorpay-Event-Id": self.event_id}

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.body)
        return data

    def again(self) -> "Webhook":
        """A redelivery: same body, same event id."""
        return self


def _unix(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def sub_id(sub: "GatewaySubscription | str") -> str:
    return sub if isinstance(sub, str) else sub.id


class FakeGateway(InMemoryGateway):
    def __init__(self, *, crash_after_creates: int | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.calls: list[tuple[str, object]] = []
        self.created = 0
        self._crash_after = crash_after_creates
        self.fail_next: dict[str, Exception] = {}  # method name -> raise once

    def _record(self, name: str, arg: object) -> None:
        self.calls.append((name, arg))
        if (exc := self.fail_next.pop(name, None)) is not None:
            raise exc

    def called(self, name: str) -> list[object]:
        return [a for n, a in self.calls if n == name]

    # --- recording overrides -------------------------------------------------------------

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        self.calls.append(("ensure_plan", spec))
        before = len(self.plans)
        plan_id = await super().ensure_plan(spec)
        self.created += len(self.plans) - before
        if self._crash_after is not None and self.created >= self._crash_after:
            self._crash_after = None
            raise RuntimeError("simulated crash after the gateway created the plan")
        return plan_id

    async def create_subscription(
        self,
        plan_id: str,
        *,
        total_count: int,
        start_at: datetime | None,
        notes: Mapping[str, str],
        lookup_first: bool = False,
    ) -> GatewaySubscription:
        self._record(
            "create_subscription", {"plan_id": plan_id, "start_at": start_at, "notes": notes}
        )
        return await super().create_subscription(
            plan_id, total_count=total_count, start_at=start_at, notes=notes
        )

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        self._record("fetch_subscription", subscription_id)
        return await super().fetch_subscription(subscription_id)

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> GatewaySubscription:
        self._record("cancel_subscription", (subscription_id, at_cycle_end))
        return await super().cancel_subscription(subscription_id, at_cycle_end=at_cycle_end)

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        self._record("fetch_payment", payment_id)
        return await super().fetch_payment(payment_id)

    async def refund_payment(
        self, payment_id: str, amount: Paise, *, notes: Mapping[str, str]
    ) -> GatewayRefund:
        self._record("refund_payment", (payment_id, int(amount), dict(notes)))
        return await super().refund_payment(payment_id, amount, notes=notes)

    # --- scripting Razorpay --------------------------------------------------------------

    def _set(self, sub: "GatewaySubscription | str", **changes: Any) -> GatewaySubscription:
        current = self.subscriptions[sub_id(sub)]
        updated = replace(current, **changes)
        self.subscriptions[current.id] = updated
        return updated

    def sign_checkout(self, payment_id: str, subscription_id: str) -> str:
        return signatures.checkout_signature(self.key_secret, payment_id, subscription_id)

    def _template(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
        return copy.deepcopy(data)

    def _sub_entity(self, sub: GatewaySubscription) -> dict[str, Any]:
        return {
            "id": sub.id,
            "entity": "subscription",
            "plan_id": sub.plan_id,
            "status": sub.status,
            "current_start": _unix(sub.current_start),
            "current_end": _unix(sub.current_end),
            "charge_at": _unix(sub.charge_at),
            "start_at": _unix(sub.start_at),
            "paid_count": sub.paid_count,
            "notes": dict(sub.notes),
            "short_url": sub.short_url,
        }

    def _webhook(self, fixture: str, **entities: dict[str, Any]) -> Webhook:
        body = self._template(fixture)
        for name, entity in entities.items():
            slot = body["payload"].setdefault(name, {"entity": {}})
            slot["entity"].update(entity)
        body["created_at"] = int(self.clock.now().timestamp())
        raw = json.dumps(body, separators=(",", ":")).encode()
        return Webhook(
            raw,
            signatures.webhook_signature(self.webhook_secret, raw),
            f"evt_fake_{next(_EVENT_SEQ):08d}",
            body["event"],
        )

    def authenticate(self, sub: "GatewaySubscription | str") -> Webhook:
        updated = self._set(sub, status="authenticated")
        return self._webhook("subscription_authenticated", subscription=self._sub_entity(updated))

    def charge(
        self, sub: "GatewaySubscription | str", amount: int, *, method: str = "card"
    ) -> Webhook:
        """A successful charge: a captured payment, the next period, status active."""
        current = self.subscriptions[sub_id(sub)]
        now = self.clock.now()
        if current.paid_count > 0 and current.current_end is not None:
            start = current.current_end
        else:
            start = current.start_at or now
        end = self.period_end(current.plan_id, start)
        pay_id = self.next_payment_id()
        fee = amount * 2 // 100
        raw = self._template("subscription_charged")["payload"]["payment"]["entity"]
        raw.update(
            id=pay_id,
            amount=amount,
            status="captured",
            method=method,
            fee=fee,
            tax=round(fee * 18 / 118),  # Razorpay's fee includes its 18% GST
            created_at=int(now.timestamp()),
            invoice_id=f"inv_{pay_id}",
            notes=[],
        )
        payment = mapping.payment(raw)
        payment = replace(payment, subscription_id=current.id)
        self.payments[pay_id] = payment
        updated = self._set(
            current,
            status="active",
            paid_count=current.paid_count + 1,
            current_start=start,
            current_end=end,
            charge_at=end,
        )
        return self._webhook(
            "subscription_charged",
            subscription=self._sub_entity(updated),
            payment=dict(payment.raw),
        )

    def fail(self, sub: "GatewaySubscription | str") -> Webhook:
        updated = self._set(sub, status="pending")
        return self._webhook("subscription_pending", subscription=self._sub_entity(updated))

    def halt(self, sub: "GatewaySubscription | str") -> Webhook:
        updated = self._set(sub, status="halted", charge_at=None)
        return self._webhook("subscription_halted", subscription=self._sub_entity(updated))

    def cancel_from_customer(self, sub: "GatewaySubscription | str") -> Webhook:
        updated = self._set(sub, status="cancelled", charge_at=None)
        return self._webhook("subscription_cancelled", subscription=self._sub_entity(updated))

    def complete(self, sub: "GatewaySubscription | str") -> Webhook:
        updated = self._set(sub, status="completed", charge_at=None)
        return self._webhook("subscription_completed", subscription=self._sub_entity(updated))

    def refund_processed(self, refund: GatewayRefund) -> Webhook:
        return self._webhook(
            "refund_processed",
            refund={
                "id": refund.id,
                "payment_id": refund.payment_id,
                "amount": int(refund.amount),
                "status": refund.status,
            },
        )

    def refund_failed(self, refund: GatewayRefund) -> Webhook:
        self.refunds[refund.id] = replace(refund, status="failed")
        return self._webhook(
            "refund_failed",
            refund={
                "id": refund.id,
                "payment_id": refund.payment_id,
                "amount": int(refund.amount),
                "status": "failed",
            },
        )

    def pay_link(self, link_id: str, *, method: str = "upi") -> Webhook:
        """The customer pays a payment link: a captured payment and `payment_link.paid`."""
        link = self.payment_links[link_id]
        now = self.clock.now()
        pay_id = self.next_payment_id()
        fee = int(link.amount) * 2 // 100
        raw = self._template("payment_link_paid")["payload"]["payment"]["entity"]
        raw.update(
            id=pay_id, amount=int(link.amount), method=method, fee=fee,
            tax=round(fee * 18 / 118), created_at=int(now.timestamp()),
        )  # fmt: skip
        self.payments[pay_id] = mapping.payment(raw)
        entity = {
            "id": link.id, "amount": int(link.amount), "status": "paid",
            "reference_id": link.reference_id, "short_url": link.short_url,
        }  # fmt: skip
        return self._webhook("payment_link_paid", payment_link=entity, payment=dict(raw))

    def payment_captured(self, payment: GatewayPayment) -> Webhook:
        return self._webhook("payment_captured", payment=dict(payment.raw))

    def pay_checkout(
        self, sub: "GatewaySubscription | str", amount: int
    ) -> tuple[Webhook, Webhook, str, str]:
        """The user finishes Razorpay Checkout: authenticated + charged webhooks, plus the
        (payment id, signature) the frontend posts to /checkout/verify."""
        authenticated = self.authenticate(sub)
        charged = self.charge(sub, amount)
        payment_id = charged.payload()["payload"]["payment"]["entity"]["id"]
        return authenticated, charged, payment_id, self.sign_checkout(payment_id, sub_id(sub))
