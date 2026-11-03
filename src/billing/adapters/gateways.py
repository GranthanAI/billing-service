"""Picks the PaymentGateway adapter from PAYMENT_GATEWAY."""

from billing.adapters.gateway_fake import InMemoryGateway
from billing.adapters.razorpay.gateway import RazorpayGateway
from billing.app.ports import Clock, PaymentGateway
from billing.settings import check_gateway_allowed


def make_gateway(
    kind: str,
    environment: str,
    *,
    key_id: str,
    key_secret: str,
    webhook_secret: str,
    clock: Clock,
) -> PaymentGateway:
    check_gateway_allowed(kind, environment)
    if kind == "fake":
        return InMemoryGateway(key_secret=key_secret, webhook_secret=webhook_secret, clock=clock)
    return RazorpayGateway(key_id, key_secret, webhook_secret, clock)
