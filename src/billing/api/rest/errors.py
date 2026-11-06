"""RFC 9457 problem details for every error the REST API returns."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from billing.app.context import correlation_id_var
from billing.app.errors import BillingError, GatewayUnavailable
from billing.app.records import StaleRecord

log = structlog.get_logger()

STATUS = {
    "VALIDATION_FAILED": 422,
    "PLAN_NOT_ALLOWED": 409,
    "SUBSCRIPTION_STATE": 409,
    "CHANGE_PENDING": 409,
    "IDEMPOTENCY_KEY_REUSED": 422,
    "REQUEST_IN_PROGRESS": 409,
    "INVALID_SIGNATURE": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "GATEWAY_UNAVAILABLE": 503,
    "GATEWAY_REJECTED": 502,
    "INTERNAL": 500,
}
PROBLEM = "application/problem+json"


def problem(code: str, title: str, detail: str, status: int | None = None) -> JSONResponse:
    http = status or STATUS.get(code, 500)
    body = {
        "type": f"https://granthan.ai/errors/{code.lower().replace('_', '-')}",
        "title": title,
        "status": http,
        "code": code,
        "detail": detail,
        "correlation_id": correlation_id_var.get(),
    }
    return JSONResponse(body, status_code=http, media_type=PROBLEM)


def problem_for(exc: BillingError, status: int | None = None) -> JSONResponse:
    return problem(exc.code, exc.title, exc.detail, status)


def install(app: FastAPI) -> None:
    @app.exception_handler(BillingError)
    async def _billing(_: Request, exc: BillingError) -> JSONResponse:
        if isinstance(exc, GatewayUnavailable):
            log.error("gateway_unavailable", detail=exc.detail)
        return problem_for(exc)

    @app.exception_handler(StaleRecord)
    async def _stale(_: Request, exc: StaleRecord) -> JSONResponse:
        return problem(
            "SUBSCRIPTION_STATE",
            "This action is not valid in the subscription's current state",
            "The subscription changed while the request ran; try again.",
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()
        )
        return problem("VALIDATION_FAILED", "The request is not valid", fields)
