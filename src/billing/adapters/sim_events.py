"""Razorpay-shaped entities and webhook bodies for the payment simulator (pure, no I/O).

The key trees follow Razorpay's documented payloads and tests/fixtures/razorpay/*.json; the parity
test compares them. Rows are plain mappings (the columns of the billing_sim tables).
"""

import hashlib
import hmac
import json
import secrets
import string
from collections.abc import Mapping
from datetime import datetime
from typing import Any

ACCOUNT_ID = "acc_SIMaccount001"
_ALPHABET = string.ascii_letters + string.digits
ID_SUFFIX_LENGTH = 14  # Razorpay ids are a prefix plus 14 characters; ours start with "SIM"


def new_id(prefix: str) -> str:
    """`prefix_` + "SIM" + 11 random characters: the same length and charset as a real id."""
    return f"{prefix}_SIM" + "".join(secrets.choice(_ALPHABET) for _ in range(ID_SUFFIX_LENGTH - 3))


def new_event_id() -> str:
    return new_id("evt")


def unix(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature(key_secret: str, payment_id: str, subscription_id: str) -> str:
    return sign(key_secret, f"{payment_id}|{subscription_id}".encode())


def order_signature(key_secret: str, order_id: str, payment_id: str) -> str:
    return sign(key_secret, f"{order_id}|{payment_id}".encode())


def payment_link_signature(
    key_secret: str, link_id: str, reference_id: str, status: str, payment_id: str
) -> str:
    """Razorpay's payment-link callback signature: link id|reference id|status|payment id."""
    return sign(key_secret, f"{link_id}|{reference_id}|{status}|{payment_id}".encode())


def fee_of(amount: int) -> tuple[int, int]:
    """(fee, tax): 2% + 18% GST of the amount (the fee includes the GST), and the GST inside it."""
    fee = (2 * amount * 236 + 10_000) // 20_000  # round_half_up(amount * 236 / 10000)
    tax = (2 * fee * 18 + 118) // 236  # round_half_up(fee * 18 / 118)
    return fee, tax


def _notes(value: Mapping[str, Any] | None, *, empty: Any) -> Any:
    return dict(value) if value else empty


def subscription_entity(row: Mapping[str, Any]) -> dict[str, Any]:
    total, paid = int(row["total_count"]), int(row["paid_count"])
    return {
        "id": row["id"],
        "entity": "subscription",
        "plan_id": row["plan_id"],
        "customer_id": "cust_SIMcustomer01",
        "status": row["status"],
        "current_start": unix(row["current_start"]),
        "current_end": unix(row["current_end"]),
        "ended_at": unix(row.get("ended_at")),
        "quantity": 1,
        "notes": _notes(row["notes"], empty={}),
        "charge_at": unix(row["charge_at"]),
        "start_at": unix(row["start_at"]),
        "end_at": None,
        "auth_attempts": 0,
        "total_count": total,
        "paid_count": paid,
        "customer_notify": True,
        "created_at": unix(row["created_at"]),
        "expire_by": None,
        "short_url": row.get("short_url"),
        "has_scheduled_changes": False,
        "change_scheduled_at": None,
        "source": "api",
        "remaining_count": max(0, total - paid),
    }


def payment_entity(row: Mapping[str, Any], *, short: bool = False) -> dict[str, Any]:
    """`short` is the reduced shape Razorpay embeds in `payment_link.paid`."""
    captured = row["status"] in ("captured", "refunded")
    common: dict[str, Any] = {
        "id": row["id"],
        "entity": "payment",
        "amount": int(row["amount"]),
        "currency": row["currency"],
        "status": row["status"],
        "order_id": row.get("order_id"),
        "invoice_id": row.get("invoice_id"),
    }
    if short:
        return {
            **common,
            "method": row.get("method"),
            "captured": captured,
            "email": "accounts@example.com",
            "contact": "+919999999999",
            "notes": _notes(row["notes"], empty=[]),
            "fee": row.get("fee"),
            "tax": row.get("tax"),
            "created_at": unix(row["created_at"]),
        }
    refunded = int(row.get("amount_refunded") or 0)
    return {
        **common,
        "international": False,
        "method": row.get("method"),
        "amount_refunded": refunded,
        "refund_status": None if not refunded else "partial",
        "captured": captured,
        "description": row.get("description"),
        "card_id": "card_SIMcard000001" if row.get("method") == "card" else None,
        "bank": None,
        "wallet": None,
        "vpa": "success@razorpay" if row.get("method") == "upi" else None,
        "email": "user@example.com",
        "contact": "+919999999999",
        "customer_id": "cust_SIMcustomer01",
        "token_id": "token_SIMtoken00001",
        "notes": _notes(row["notes"], empty=[]),
        "fee": row.get("fee"),
        "tax": row.get("tax"),
        "error_code": row.get("error_code"),
        "error_description": row.get("error_description"),
        "created_at": unix(row["created_at"]),
    }


def refund_entity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "entity": "refund",
        "amount": int(row["amount"]),
        "currency": "INR",
        "payment_id": row["payment_id"],
        "notes": _notes(row["notes"], empty={}),
        "receipt": None,
        "acquirer_data": {"arn": None},
        "created_at": unix(row["created_at"]),
        "batch_id": None,
        "status": row["status"],
        "speed_processed": "normal",
        "speed_requested": "normal",
    }


def payment_link_entity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "amount": int(row["amount"]),
        "currency": "INR",
        "status": row["status"],
        "reference_id": row.get("reference_id") or "",
        "short_url": row["short_url"],
        "accept_partial": False,
    }


def order_entity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "entity": "order",
        "amount": int(row["amount"]),
        "amount_paid": int(row["amount"]) if row["status"] == "paid" else 0,
        "currency": "INR",
        "receipt": row.get("receipt"),
        "status": row["status"],
        "notes": _notes(row["notes"], empty=[]),
        "created_at": unix(row["created_at"]),
    }


CONTAINS: Mapping[str, tuple[str, ...]] = {
    "subscription.charged": ("subscription", "payment"),
    "subscription.completed": ("subscription", "payment"),
    "payment.captured": ("payment",),
    "payment.failed": ("payment",),
    "payment.authorized": ("payment",),
    "refund.processed": ("refund", "payment"),
    "refund.failed": ("refund", "payment"),
    "order.paid": ("order", "payment"),
    "payment_link.paid": ("payment_link", "order", "payment"),
}


def build_body(event: str, now: datetime, **entities: Mapping[str, Any]) -> dict[str, Any]:
    """The webhook body for `event`. `entities` are the rendered entities by name; `order` is
    listed in `contains` of payment_link.paid but, as in Razorpay's own sample, not in the payload.
    """
    contains = CONTAINS.get(event, (event.split(".")[0],))
    payload = {name: {"entity": entities[name]} for name in contains if name in entities}
    return {
        "entity": "event",
        "account_id": ACCOUNT_ID,
        "event": event,
        "contains": list(contains),
        "payload": payload,
        "created_at": unix(now),
    }


def encode(body: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed and delivered."""
    return json.dumps(body, separators=(",", ":")).encode()
