"""Recording fake PaymentGateway. Phase 6 extends it with subscriptions, payments and webhooks."""

from billing.adapters.gateway_fake import InMemoryGateway
from billing.app.ports import GatewayPlanSpec


class FakeGateway(InMemoryGateway):
    def __init__(self, *, crash_after_creates: int | None = None) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []
        self.created = 0
        self._crash_after = crash_after_creates

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        self.calls.append(("ensure_plan", spec))
        before = len(self.plans)
        plan_id = await super().ensure_plan(spec)
        self.created += len(self.plans) - before
        if self._crash_after is not None and self.created >= self._crash_after:
            self._crash_after = None
            raise RuntimeError("simulated crash after the gateway created the plan")
        return plan_id
