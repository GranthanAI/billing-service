"""P1a: simulation can never run in production, and simulated/real money never share a database."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from billing.adapters.clock import SystemClock
from billing.adapters.db.gateway_mode import GatewayModeMismatch, ensure_gateway_mode
from billing.adapters.gateways import make_gateway
from billing.app.records import PaymentRecord
from billing.domain.catalog import OwnerType
from billing.domain.money import Paise
from billing.settings import check_simulation_allowed
from tests.integration.conftest import FreshDb

KEYS = dict(key_id="k", key_secret="s", webhook_secret="w", clock=SystemClock())


def test_simulation_is_refused_in_production() -> None:
    with pytest.raises(ValueError, match="PAYMENT_SIMULATION"):
        check_simulation_allowed(True, "production")
    check_simulation_allowed(True, "staging")
    check_simulation_allowed(False, "production")


@pytest.mark.parametrize("kind", ["razorpay", "fake"])
def test_make_gateway_refuses_simulation_in_production(kind: str) -> None:
    with pytest.raises(ValueError, match="PAYMENT_SIMULATION"):
        make_gateway(kind, "production", simulation=True, engine=object(), **KEYS)  # type: ignore[arg-type]


def test_make_gateway_picks_the_simulator_over_payment_gateway() -> None:
    from billing.adapters.gateway_sim import SimulatorGateway

    gw = make_gateway("razorpay", "development", simulation=True, engine=object(), **KEYS)  # type: ignore[arg-type]
    assert isinstance(gw, SimulatorGateway)
    from billing.adapters.gateway_fake import InMemoryGateway

    assert isinstance(make_gateway("fake", "development", **KEYS), InMemoryGateway)


async def _take_a_payment(db: FreshDb, gateway: str) -> None:
    async with db.app_uow() as uow:
        account = await uow.accounts.get_or_create(OwnerType.USER, uuid4(), "u@example.com")
        await uow.payments.insert(
            PaymentRecord(
                id=uuid4(),
                billing_account_id=account.id,
                purpose="subscription",
                gateway_payment_id=f"pay_{uuid4().hex[:14]}",
                amount_paise=Paise(49900),
                status="captured",
                captured_at=datetime(2026, 11, 1, tzinfo=UTC),
                raw={},
                gateway=gateway,
            )
        )


async def test_first_start_records_the_mode_and_same_mode_is_fine(fresh_db: FreshDb) -> None:
    await ensure_gateway_mode(fresh_db.app_engine, "simulator", allow_switch=False)
    await ensure_gateway_mode(fresh_db.app_engine, "simulator", allow_switch=False)
    assert await _mode(fresh_db) == "simulator"


async def test_switching_without_payments_is_allowed(fresh_db: FreshDb) -> None:
    await ensure_gateway_mode(fresh_db.app_engine, "simulator", allow_switch=False)
    await ensure_gateway_mode(fresh_db.app_engine, "razorpay", allow_switch=False)
    assert await _mode(fresh_db) == "razorpay"


async def test_switching_with_payments_refuses_to_start(fresh_db: FreshDb) -> None:
    await ensure_gateway_mode(fresh_db.app_engine, "simulator", allow_switch=False)
    await _take_a_payment(fresh_db, "simulator")
    with pytest.raises(GatewayModeMismatch, match="simulator"):
        await ensure_gateway_mode(fresh_db.app_engine, "razorpay", allow_switch=False)
    assert await _mode(fresh_db) == "simulator"  # nothing changed


async def test_the_override_allows_the_switch(fresh_db: FreshDb) -> None:
    await ensure_gateway_mode(fresh_db.app_engine, "simulator", allow_switch=False)
    await _take_a_payment(fresh_db, "simulator")
    await ensure_gateway_mode(fresh_db.app_engine, "razorpay", allow_switch=True)
    assert await _mode(fresh_db) == "razorpay"


async def test_the_payment_row_remembers_its_gateway(fresh_db: FreshDb) -> None:
    await _take_a_payment(fresh_db, "simulator")
    from sqlalchemy import text

    async with fresh_db.app_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT gateway FROM billing.payments"))).all()
    assert [r[0] for r in rows] == ["simulator"]


async def _mode(db: FreshDb) -> str:
    from sqlalchemy import text

    async with db.app_engine.connect() as conn:
        return str((await conn.execute(text("SELECT mode FROM billing.gateway_mode"))).scalar())
