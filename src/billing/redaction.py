"""Strip personal data from Razorpay payloads (and anything else) before it is logged or sent to
Sentry. Pure Python, no imports from the rest of the service."""

import re
from collections.abc import Mapping, MutableMapping
from typing import Any

REDACTED = "[redacted]"
# Razorpay payment entities carry these; webhook_inbox keeps the full payload, logs never do.
SENSITIVE_KEYS = frozenset(
    {
        "email",
        "billing_email",
        "customer_email",
        "contact",
        "phone",
        "customer_contact",
        "vpa",
        "card",
        "card_id",
        "card_number",
        "cvv",
        "account_number",
        "ifsc",
        "token_id",
    }
)
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def redact(value: Any) -> Any:
    """A copy of `value` with sensitive keys blanked and e-mail addresses masked in strings."""
    if isinstance(value, Mapping):
        return {
            k: REDACTED if str(k).lower() in SENSITIVE_KEYS else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return EMAIL.sub(REDACTED, value)
    return value


def structlog_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    result: dict[str, Any] = redact(dict(event_dict))
    return result


def sentry_before_send(event: dict[str, Any], hint: Any) -> dict[str, Any] | None:
    result: dict[str, Any] = redact(event)
    return result
