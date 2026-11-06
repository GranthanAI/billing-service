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


class PlanNotAllowed(BillingError):
    code = "PLAN_NOT_ALLOWED"
    title = "This plan is not available for your account"


class SubscriptionStateError(BillingError):
    code = "SUBSCRIPTION_STATE"
    title = "This action is not valid in the subscription's current state"


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


class Forbidden(BillingError):
    code = "FORBIDDEN"
    title = "This action needs a different staff role"


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
