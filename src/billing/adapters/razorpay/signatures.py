"""HMAC-SHA256 signatures, compared in constant time."""

import hashlib
import hmac


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature(key_secret: str, payment_id: str, subscription_id: str) -> str:
    return _hex(key_secret, f"{payment_id}|{subscription_id}".encode())


def webhook_signature(webhook_secret: str, raw_body: bytes) -> str:
    return _hex(webhook_secret, raw_body)


def verify_checkout(key_secret: str, payment_id: str, subscription_id: str, signature: str) -> bool:
    expected = checkout_signature(key_secret, payment_id, subscription_id)
    return hmac.compare_digest(expected.encode(), signature.encode())


def verify_webhook(webhook_secret: str, raw_body: bytes, signature: str) -> bool:
    """`webhook_secret` may be several secrets joined by commas (during a rotation: the new one
    and the old one both verify). Every secret is tried, so timing does not reveal which."""
    ok = False
    for secret in (s for s in webhook_secret.split(",") if s):
        expected = webhook_signature(secret, raw_body)
        ok |= hmac.compare_digest(expected.encode(), signature.encode())
    return ok
