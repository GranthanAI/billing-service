"""A SimulatorGateway that also plays the FakeGateway scripting API, so the lifecycle tests written
for the fake run unchanged against the simulator.

The tests call `env.gateway.charge(...)`, `.fail(...)`, `.halt(...)` and friends *synchronously*
(and sometimes drop the returned webhook: "its webhook was lost"). The simulator is async and
Postgres-backed, so the scripted operations run on a private event loop in a worker thread with its
own engine; the protocol methods the application calls keep using the test's own engine.
"""

import asyncio
import threading
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from billing.adapters.gateway_sim import SimConfig, SimulatorGateway
from billing.app.ports import (
    Clock,
    GatewayPayment,
    GatewayRefund,
    GatewaySubscription,
)
from tests.support.fake_gateway import Webhook

T = TypeVar("T")
CARD = {"method": "card", "card_number": "4111111111111111", "otp": "123456"}


class SimDriver(SimulatorGateway):
    def __init__(
        self,
        engine: AsyncEngine,
        script_url: str,
        *,
        clock: Clock,
        config: SimConfig,
        key_id: str = "rzp_sim_key",
        key_secret: str = "sim_key_secret",
        webhook_secret: str = "sim_webhook_secret",
    ) -> None:
        super().__init__(
            engine,
            key_id=key_id,
            key_secret=key_secret,
            webhook_secret=webhook_secret,
            clock=clock,
            config=config,
        )
        self.calls: list[tuple[str, object]] = []
        self.plans: dict[
            str, object
        ] = {}  # the fake has plan specs here; Env.set_period edits them
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._script: SimulatorGateway = self._on_loop(self._make_script(script_url, clock, config))

    # --- the private loop ----------------------------------------------------------------

    def _on_loop(self, coro: Awaitable[T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)  # type: ignore[arg-type]

    async def _make_script(self, url: str, clock: Clock, config: SimConfig) -> SimulatorGateway:
        return SimulatorGateway(
            create_async_engine(url),
            key_id=self.key_id,
            key_secret=self.key_secret,
            webhook_secret=self.webhook_secrets,
            clock=clock,
            config=config,
        )

    def close(self) -> None:
        self._on_loop(self._script._engine.dispose())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # --- recording the calls the application makes (the poll test counts them) -----------

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        self.calls.append(("fetch_subscription", subscription_id))
        return await super().fetch_subscription(subscription_id)

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        self.calls.append(("fetch_payment", payment_id))
        return await super().fetch_payment(payment_id)

    async def list_subscription_payments(self, subscription_id: str) -> list[GatewayPayment]:
        self.calls.append(("list_subscription_payments", subscription_id))
        return await super().list_subscription_payments(subscription_id)

    async def refund_payment(self, payment_id: str, amount: Any, *, notes: Mapping[str, str]):  # type: ignore[no-untyped-def]
        self.calls.append(("refund_payment", (payment_id, int(amount), dict(notes))))
        return await super().refund_payment(payment_id, amount, notes=notes)

    def called(self, name: str) -> list[object]:
        return [a for n, a in self.calls if n == name]

    # --- scripting Razorpay, like FakeGateway -------------------------------------------

    async def _outbox_after(self, last_id: int) -> list[Webhook]:
        async with self._script._engine.connect() as conn:
            rows = await self._script._all(
                conn,
                "SELECT id, event_id, event, body, signature FROM billing_sim.sim_webhook_outbox "
                "WHERE id > :i ORDER BY id",
                i=last_id,
            )
        return [Webhook(bytes(r["body"]), r["signature"], r["event_id"], r["event"]) for r in rows]

    async def _last_id(self) -> int:
        async with self._script._engine.connect() as conn:
            row = await self._script._one(
                conn, "SELECT coalesce(max(id), 0) AS n FROM billing_sim.sim_webhook_outbox"
            )
        return int(row["n"]) if row else 0

    def _scripted(self, op: Awaitable[Any]) -> list[Webhook]:
        async def go() -> list[Webhook]:
            before = await self._last_id()
            await op
            return await self._outbox_after(before)

        return self._on_loop(go())

    @staticmethod
    def _sid(sub: "GatewaySubscription | str") -> str:
        return sub if isinstance(sub, str) else sub.id

    @staticmethod
    def _pick(hooks: list[Webhook], event: str) -> Webhook:
        found = [h for h in hooks if h.event_type == event]
        assert found, f"the simulator sent {[h.event_type for h in hooks]}, not {event}"
        return found[-1]

    def authenticate(self, sub: "GatewaySubscription | str") -> Webhook:
        hooks = self._scripted(self._script.pay(self._sid(sub), **CARD))
        return self._pick(hooks, "subscription.authenticated")

    def charge(
        self, sub: "GatewaySubscription | str", amount: int, *, method: str = "card"
    ) -> Webhook:
        """A successful charge now (the plan's price is what the simulator charges)."""
        sid = self._sid(sub)

        async def go() -> None:
            await self._script.set_next_outcome(sid, "success")
            await self._script.renew(sid, force=True)

        return self._pick(self._scripted(go()), "subscription.charged")

    def fail(self, sub: "GatewaySubscription | str") -> Webhook:
        sid = self._sid(sub)

        async def go() -> None:
            await self._script.set_next_outcome(sid, "fail")
            await self._script.renew(sid, force=True)

        return self._pick(self._scripted(go()), "subscription.pending")

    def halt(self, sub: "GatewaySubscription | str") -> Webhook:
        sid = self._sid(sub)

        async def go() -> None:
            await self._script.set_next_outcome(sid, "halt")
            await self._script.renew(sid, force=True)

        return self._pick(self._scripted(go()), "subscription.halted")

    def cancel_from_customer(self, sub: "GatewaySubscription | str") -> Webhook:
        hooks = self._scripted(self._script.customer_cancel(self._sid(sub)))
        return self._pick(hooks, "subscription.cancelled")

    def complete(self, sub: "GatewaySubscription | str") -> Webhook:
        raise NotImplementedError("the simulator completes a subscription by itself")

    def refund_failed(self, refund: GatewayRefund) -> Webhook:
        hooks = self._scripted(self._script.fail_refund(refund.id))
        return self._pick(hooks, "refund.failed")

    def pay_checkout(
        self, sub: "GatewaySubscription | str", amount: int
    ) -> tuple[Webhook, Webhook, str, str]:
        """The user finishes checkout: (authenticated, charged, payment id, signature)."""
        sid = self._sid(sub)
        holder: dict[str, Any] = {}

        async def go() -> None:
            holder["result"] = await self._script.pay(sid, **CARD)

        hooks = self._scripted(go())
        result = holder["result"]
        assert result.ok and result.handler is not None, result.error
        charged = self._pick(hooks, "subscription.charged")
        return (
            self._pick(hooks, "subscription.authenticated"),
            charged,
            result.handler["razorpay_payment_id"],
            result.handler["razorpay_signature"],
        )

    def sign_checkout(self, payment_id: str, subscription_id: str) -> str:
        from billing.adapters.sim_events import checkout_signature

        return checkout_signature(self.key_secret, payment_id, subscription_id)

    def now(self) -> datetime:
        return self._clock.now()
