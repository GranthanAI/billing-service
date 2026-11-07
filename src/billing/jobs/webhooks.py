"""Webhook processor: drains webhook_inbox. Several replicas are safe (SKIP LOCKED + a lease)."""

import asyncio
import contextlib
from typing import Any

import structlog
from opentelemetry import trace

from billing.adapters.razorpay import mapping
from billing.app.config import BillingContext
from billing.app.ports import GatewayPayment, GatewaySubscription
from billing.app.records import InboxRow
from billing.app.webhooks import EventApplier, GatewayEvent
from billing.observability import PAYMENTS, WEBHOOK_FAILED

log = structlog.get_logger()
tracer = trace.get_tracer("billing")

BATCH = 20
LEASE_SECONDS = 300
MAX_ATTEMPTS = 10
# Events whose trigger never depends on a fresh read: the payload's own entity is enough.
NO_FETCH = frozenset(
    {
        "subscription.authenticated",
        "subscription.activated",
        "subscription.completed",
        "subscription.expired",
    }
)


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    found = payload.get("payload", {}).get(name, {}).get("entity")
    return found if isinstance(found, dict) else None


class WebhookProcessor:
    def __init__(
        self, bctx: BillingContext, applier: EventApplier, *, poll_seconds: float = 1.0
    ) -> None:
        self._b = bctx
        self._applier = applier
        self._poll = poll_seconds

    async def _load(self, row: InboxRow) -> GatewayEvent:
        """Fetch what the event needs from Razorpay, outside any transaction."""
        body = dict(row.payload)
        kind = row.event_type
        gw = self._b.gateway
        if kind.startswith("subscription."):
            raw = _entity(body, "subscription")
            if raw is None:
                return GatewayEvent(row.event_id, kind)
            snapshot: GatewaySubscription = mapping.subscription(raw)
            fresh = snapshot if kind in NO_FETCH else await gw.fetch_subscription(snapshot.id)
            payment: GatewayPayment | None = None
            if kind == "subscription.charged":
                pay = _entity(body, "payment")
                if pay is None:
                    raise ValueError("subscription.charged without a payment entity")
                payment = await gw.fetch_payment(str(pay["id"]))
            return GatewayEvent(row.event_id, kind, fresh, snapshot, payment)
        if kind.startswith("refund."):
            raw_refund = _entity(body, "refund")
            if raw_refund is None:
                return GatewayEvent(row.event_id, kind)
            return GatewayEvent(
                row.event_id, kind, refund=await gw.fetch_refund(str(raw_refund["id"]))
            )
        if kind == "payment_link.paid":
            link, pay = _entity(body, "payment_link"), _entity(body, "payment")
            if link is None or pay is None:
                raise ValueError("payment_link.paid without a payment_link and a payment entity")
            payment = await gw.fetch_payment(str(pay["id"]))
            return GatewayEvent(
                row.event_id, kind, payment=payment, payment_link_id=str(link["id"])
            )
        return GatewayEvent(row.event_id, kind)  # payment.* and the rest need no action

    async def _process(self, row: InboxRow) -> None:
        try:
            with tracer.start_as_current_span("billing.webhook.process") as span:
                span.set_attribute("webhook.event_type", row.event_type)
                event = await self._load(row)
                async with self._b.uow_factory() as uow:
                    outcome = await self._applier.apply(uow, event)
                    await uow.inbox.mark(row.id, outcome)
            if row.event_type == "payment.failed":
                entity = _entity(dict(row.payload), "payment") or {}
                PAYMENTS.labels(
                    method=str(entity.get("method") or "unknown"), status="failed"
                ).inc()
        except Exception as exc:
            async with self._b.uow_factory() as uow:
                status = await uow.inbox.record_failure(
                    row.id, f"{type(exc).__name__}: {exc}", self._b.clock.now(), MAX_ATTEMPTS
                )
            if status == "failed":
                WEBHOOK_FAILED.inc()
                log.error("webhook_failed", event_id=row.event_id, error=str(exc))
            else:
                log.warning("webhook_retry", event_id=row.event_id, error=str(exc))

    async def process_pending(self) -> int:
        """One pass: claim up to 20 due rows and process them. Returns how many were claimed."""
        async with self._b.uow_factory() as uow:
            rows = await uow.inbox.claim_due(BATCH, LEASE_SECONDS, self._b.clock.now())
        for row in rows:
            await self._process(row)
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                claimed = await self.process_pending()
            except Exception:
                log.exception("webhook_pass_failed")
                claimed = 0
            if claimed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll)
