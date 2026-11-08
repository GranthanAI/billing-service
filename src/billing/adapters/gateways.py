"""Picks the PaymentGateway adapter: the simulator when PAYMENT_SIMULATION is on, else from
PAYMENT_GATEWAY."""

from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.gateway_fake import InMemoryGateway
from billing.adapters.gateway_sim import SimConfig, SimulatorGateway
from billing.adapters.razorpay.gateway import RazorpayGateway
from billing.app.ports import Clock, PaymentGateway
from billing.settings import Settings, check_gateway_allowed, check_simulation_allowed


def sim_config(s: Settings) -> SimConfig:
    return SimConfig(
        month_seconds=s.sim_month_seconds,
        retry_interval=s.sim_retry_interval,
        refund_delay=s.sim_refund_delay_seconds,
        webhook_url=s.sim_webhook_url,
        webhook_delay=s.sim_webhook_delay_seconds,
        duplicate_pct=s.sim_webhook_duplicate_pct,
        drop_pct=s.sim_webhook_drop_pct,
        reorder=s.sim_webhook_reorder,
    )


def make_gateway(
    kind: str,
    environment: str,
    *,
    key_id: str,
    key_secret: str,
    webhook_secret: str,
    clock: Clock,
    simulation: bool = False,
    engine: AsyncEngine | None = None,
    sim: SimConfig | None = None,
) -> PaymentGateway:
    check_simulation_allowed(simulation, environment)
    if simulation:
        if engine is None:
            raise ValueError("the payment simulator needs the database engine")
        return SimulatorGateway(
            engine,
            key_id=key_id,
            key_secret=key_secret,
            webhook_secret=webhook_secret,
            clock=clock,
            config=sim,
        )
    check_gateway_allowed(kind, environment)
    if kind == "fake":
        return InMemoryGateway(key_secret=key_secret, webhook_secret=webhook_secret, clock=clock)
    return RazorpayGateway(key_id, key_secret, webhook_secret, clock)


def gateway_mode(s: Settings) -> str:
    """What the gateway-mode guard records: simulator | razorpay | fake."""
    return "simulator" if s.payment_simulation else s.payment_gateway
