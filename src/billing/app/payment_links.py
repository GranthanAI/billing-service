"""The "Pay now" link of a subscription: one definition for /me and for the emails."""

from uuid import UUID

from billing.app.errors import GatewayRejected, GatewayUnavailable
from billing.app.ports import PaymentGateway


async def payment_action_url(
    gateway: PaymentGateway, user_id: UUID, gateway_subscription_id: str
) -> str | None:
    """The gateway subscription's short_url; in simulation a signed link to the simulated page."""
    signed = getattr(gateway, "signed_url", None)
    if signed is not None:
        return str(signed(gateway_subscription_id, str(user_id)))
    try:
        return (await gateway.fetch_subscription(gateway_subscription_id)).short_url
    except (GatewayRejected, GatewayUnavailable):
        return None
