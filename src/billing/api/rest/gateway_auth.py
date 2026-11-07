"""The REST trust boundary: only api-gateway may call billing's routes.

`X-User-Id`, `X-Staff-Role` and `X-Correlation-ID` are trusted because api-gateway set them after
verifying the JWT. That is only safe if nothing else can reach billing with forged headers, so every
route except the public ones needs `X-Gateway-Secret` to match a configured secret (several during
a rotation). Without a match the request is answered 401 here and no handler, idempotency record or
database write happens.
"""

import hmac

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from billing.api.rest.errors import problem

log = structlog.get_logger()

HEADER = b"x-gateway-secret"
# Public: the pricing page, the model list, Razorpay's webhook (it has its own signature) and the
# probes. /metrics is scraped inside the cluster network by Prometheus, which has no gateway secret.
PUBLIC_PATHS = frozenset(
    {
        "/billing/v1/plans",
        "/billing/v1/models",
        "/billing/v1/webhooks/razorpay",
        "/healthz",
        "/readyz",
        "/metrics",
    }
)


def secret_matches(presented: bytes, secrets: tuple[str, ...]) -> bool:
    """Constant time against every configured secret: which one matched is not revealed."""
    ok = False
    for secret in secrets:
        ok |= hmac.compare_digest(secret.encode(), presented)
    return ok


class GatewayAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] in PUBLIC_PATHS
            or scope["path"].startswith("/internal/")
        ):
            await self.app(scope, receive, send)
            return
        secrets: tuple[str, ...] = getattr(scope["app"].state, "gateway_secrets", ())
        presented = next((v for k, v in scope["headers"] if k.lower() == HEADER), b"")
        # No secret configured means nobody can authenticate: closed, never open.
        if not secrets or not secret_matches(presented, secrets):
            log.warning("gateway_auth_rejected", path=scope["path"], method=scope["method"])
            response = problem(
                "UNAUTHENTICATED",
                "Gateway authentication failed",
                "Missing or invalid credentials.",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
