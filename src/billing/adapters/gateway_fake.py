"""In-memory PaymentGateway for local runs (PAYMENT_GATEWAY=fake) and as the test fake's base.

Only what Phase 3 needs; the real Razorpay adapter is Phase 6.
"""

import itertools

from billing.app.ports import GatewayPlanSpec


class InMemoryGateway:
    def __init__(self) -> None:
        self.plans: dict[str, GatewayPlanSpec] = {}
        self._seq = itertools.count(1)

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        checksum, interval = spec.notes.get("checksum"), spec.notes.get("interval")
        for plan_id, existing in self.plans.items():
            if (
                existing.notes.get("checksum") == checksum
                and existing.notes.get("interval") == interval
            ):
                return plan_id
        plan_id = f"plan_fake_{next(self._seq):04d}"
        self.plans[plan_id] = spec
        return plan_id
