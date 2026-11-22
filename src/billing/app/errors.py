"""Application errors. Each carries a stable `code` from the LLD's error table; the REST layer
maps code to HTTP status and renders RFC 9457 problem details."""


class BillingError(Exception):
    code = "INTERNAL"
    title = "Internal error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title


class ValidationFailed(BillingError):
    code = "VALIDATION_FAILED"
    title = "The request is not valid"


class CatalogInvalid(ValidationFailed):
    """A catalog draft broke a rule; `errors` lists every problem (each names the plan and the field)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class CatalogConflict(BillingError):
    code = "CATALOG_CONFLICT"
    title = "The catalog changed since this draft was made"


class PlanNotAllowed(BillingError):
    code = "PLAN_NOT_ALLOWED"
    title = "This plan is not available for your account"


class SubscriptionStateError(BillingError):
    code = "SUBSCRIPTION_STATE"
    title = "This action is not valid in the subscription's current state"


class PlanGatewayMismatch(BillingError):
    code = "PLAN_GATEWAY_MISMATCH"
    title = "The plan is not set up on the active payment gateway"


class ChangePending(BillingError):
    code = "CHANGE_PENDING"
    title = "A plan change is already waiting for authorisation"


class InvalidSignature(BillingError):
    code = "INVALID_SIGNATURE"
    title = "The payment could not be verified"


class IdempotencyKeyReused(BillingError):
    code = "IDEMPOTENCY_KEY_REUSED"
    title = "The Idempotency-Key was already used for a different request"


class RequestInProgress(BillingError):
    code = "REQUEST_IN_PROGRESS"
    title = "A request with this Idempotency-Key is still running"


class Unauthenticated(BillingError):
    code = "UNAUTHENTICATED"
    title = "X-User-Id is missing or not a UUID"


class NotFound(BillingError):
    code = "NOT_FOUND"
    title = "Not found"


class NoSubscription(NotFound):
    """The organisation has no billing account, or no live subscription (an org-only plan
    override does not count: org-service manages pilot seats itself). organisation-service
    tells this 404 from "the route does not exist" by the code."""

    code = "NO_SUBSCRIPTION"
    title = "This organisation has no subscription"


class Forbidden(BillingError):
    code = "FORBIDDEN"
    title = "This action needs a different staff role"


class OrgDirectoryUnavailable(BillingError):
    """org-service could not be asked (timeout, 5xx). Entitlements fall back to personal plans."""

    code = "ORG_DIRECTORY_UNAVAILABLE"
    title = "The organisation directory is unavailable"


class GatewayUnavailable(BillingError):
    """Razorpay timed out, was unreachable, or kept answering 429/5xx."""

    code = "GATEWAY_UNAVAILABLE"
    title = "The payment gateway is unavailable"


class GatewayRejected(BillingError):
    """Razorpay answered with a 4xx: the request itself was refused."""

    code = "GATEWAY_REJECTED"
    title = "The payment gateway rejected the request"

    def __init__(self, gateway_code: str, description: str) -> None:
        super().__init__(f"{gateway_code}: {description}")
        self.gateway_code = gateway_code
        self.description = description
