"""Razorpay JSON -> the port's frozen DTOs. Unix seconds become aware UTC datetimes."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from billing.app.ports import GatewayPayment, GatewayRefund, GatewaySubscription
from billing.domain.money import Paise


def ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _notes(value: Any) -> Mapping[str, str]:
    # Razorpay returns [] (not {}) for empty notes.
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _paise(value: Any) -> Paise | None:
    return Paise(int(value)) if value is not None else None


def subscription(d: Mapping[str, Any]) -> GatewaySubscription:
    return GatewaySubscription(
        id=d["id"],
        plan_id=d["plan_id"],
        status=d["status"],
        current_start=ts(d.get("current_start")),
        current_end=ts(d.get("current_end")),
        charge_at=ts(d.get("charge_at")),
        start_at=ts(d.get("start_at")),
        paid_count=int(d.get("paid_count") or 0),
        notes=_notes(d.get("notes")),
        short_url=d.get("short_url"),
    )


def payment(d: Mapping[str, Any]) -> GatewayPayment:
    return GatewayPayment(
        id=d["id"],
        amount=Paise(int(d["amount"])),
        currency=d.get("currency", "INR"),
        status=d["status"],
        method=d.get("method"),
        fee=_paise(d.get("fee")),
        tax=_paise(d.get("tax")),
        order_id=d.get("order_id"),
        invoice_id=d.get("invoice_id"),
        subscription_id=d.get("subscription_id"),
        created_at=ts(d["created_at"]) or datetime.fromtimestamp(0, UTC),
        notes=_notes(d.get("notes")),
        raw=dict(d),
    )


def refund(d: Mapping[str, Any]) -> GatewayRefund:
    return GatewayRefund(
        id=d["id"],
        payment_id=d["payment_id"],
        amount=Paise(int(d["amount"])),
        status=d.get("status", "processed"),
        notes=_notes(d.get("notes")),
    )
