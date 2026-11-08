"""P1b: simulator webhook bodies have the same key tree as the recorded Razorpay fixtures."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from billing.adapters import sim_events as ev

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "razorpay"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)

SUB: dict[str, Any] = {
    "id": "sub_SIMabcdefghi", "plan_id": "plan_SIMabcdefgh", "status": "active",
    "current_start": NOW, "current_end": NOW, "charge_at": NOW, "start_at": NOW,
    "paid_count": 1, "total_count": 12, "notes": {"billing_ref": "x"}, "short_url": None,
    "created_at": NOW, "ended_at": None,
}  # fmt: skip
PAY: dict[str, Any] = {
    "id": "pay_SIMabcdefghi", "amount": 49900, "currency": "INR", "status": "captured",
    "method": "card", "fee": 1178, "tax": 180, "order_id": "order_SIMabcdefg",
    "invoice_id": "inv_SIMabcdefghi", "notes": {}, "description": "Pro", "created_at": NOW,
    "amount_refunded": 0, "error_code": None, "error_description": None,
}  # fmt: skip
REFUND = {"id": "rfnd_SIMabcdefg", "amount": 100, "payment_id": PAY["id"], "notes": {},
          "status": "processed", "created_at": NOW}  # fmt: skip
LINK = {"id": "plink_SIMabcdefg", "amount": 500, "status": "paid", "reference_id": "r",
        "short_url": "/x"}  # fmt: skip


def key_tree(value: Any, key: str = "") -> Any:
    """Dict keys, recursively. `notes` holds free-form data (and Razorpay sends [] for none)."""
    if isinstance(value, dict) and key != "notes":
        return {k: key_tree(v, k) for k, v in value.items()}
    return None


def simulated(event: str, entities: list[str]) -> dict[str, Any]:
    makers: dict[str, Any] = {
        "subscription": lambda: ev.subscription_entity({**SUB, "status": event.split(".")[1]}),
        "payment": lambda: ev.payment_entity(PAY, short=event == "payment_link.paid"),
        "refund": lambda: ev.refund_entity(REFUND),
        "payment_link": lambda: ev.payment_link_entity(LINK),
    }
    return ev.build_body(event, NOW, **{name: makers[name]() for name in entities})


FIXTURE_FILES = sorted(p for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_simulated_body_has_the_fixtures_key_tree(path: Path) -> None:
    fixture = json.loads(path.read_text())
    entities = [name for name in fixture["payload"]]
    body = simulated(fixture["event"], entities)
    assert key_tree(body) == key_tree(fixture)
    assert body["contains"] == fixture["contains"]
    assert body["entity"] == "event" and body["event"] == fixture["event"]


def test_every_fixture_event_type_is_covered() -> None:
    assert len(FIXTURE_FILES) >= 14  # the files this parity suite was written against


def test_encode_is_the_exact_signed_form() -> None:
    body = simulated("subscription.activated", ["subscription"])
    raw = ev.encode(body)
    assert json.loads(raw) == json.loads(json.dumps(body))
    assert ev.sign("s", raw) == ev.sign("s", ev.encode(body))
