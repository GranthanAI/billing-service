"""The `provider` (and, in simulation, `simulator_url`) fields on every checkout-like response."""

from uuid import UUID

from fastapi import Request

from billing.adapters.gateway_sim import SimulatorGateway


def provider_fields(request: Request, gateway_id: str, user: UUID) -> dict[str, str]:
    """Razorpay mode: just the provider. Simulation: also the signed link to the simulated page."""
    gateway = request.app.state.gateway
    if isinstance(gateway, SimulatorGateway):
        return {
            "provider": "simulator",
            "simulator_url": gateway.signed_url(gateway_id, str(user)),
        }
    return {"provider": "razorpay"}
