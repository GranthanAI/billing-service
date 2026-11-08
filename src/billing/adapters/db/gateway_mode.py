"""Gateway-mode guard: simulated and real money must never share a database (plan 5.2)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class GatewayModeMismatch(RuntimeError):
    pass


async def ensure_gateway_mode(engine: AsyncEngine, mode: str, *, allow_switch: bool) -> None:
    """Record `mode` on the first start. At a later start with a different mode, refuse when
    `billing.payments` has rows, unless `allow_switch` (ALLOW_GATEWAY_MODE_SWITCH=true)."""
    async with engine.begin() as conn:
        await conn.execute(text("LOCK TABLE billing.gateway_mode IN EXCLUSIVE MODE"))
        recorded = (await conn.execute(text("SELECT mode FROM billing.gateway_mode"))).scalar()
        if recorded is None:
            await conn.execute(
                text("INSERT INTO billing.gateway_mode (mode) VALUES (:m)"), {"m": mode}
            )
            return
        if recorded == mode:
            return
        has_payments = (
            await conn.execute(text("SELECT EXISTS (SELECT 1 FROM billing.payments)"))
        ).scalar()
        if has_payments and not allow_switch:
            raise GatewayModeMismatch(
                f"this database took payments in gateway mode {recorded!r} and is now started in "
                f"{mode!r}; refusing to mix them (ALLOW_GATEWAY_MODE_SWITCH=true overrides)"
            )
        await conn.execute(
            text("UPDATE billing.gateway_mode SET mode = :m, set_at = now()"), {"m": mode}
        )
