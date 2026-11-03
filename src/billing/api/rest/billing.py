"""Authenticated billing routes. The user comes from X-User-Id (set by api-gateway)."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing.api.rest.idempotency import parse_user_id
from billing.app.errors import Unauthenticated
from billing.app.subscriptions import CheckoutOptions, SubscriptionService
from billing.domain.catalog import Interval

router = APIRouter(prefix="/billing/v1")

GSTIN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"


def current_user(request: Request) -> UUID:
    user = parse_user_id(request.headers.get("X-User-Id"))
    if user is None:
        raise Unauthenticated()
    return user


User = Annotated[UUID, Depends(current_user)]


def service(request: Request) -> SubscriptionService:
    svc: SubscriptionService = request.app.state.subscriptions
    return svc


Svc = Annotated[SubscriptionService, Depends(service)]


class CheckoutBody(BaseModel):
    plan_code: str
    interval: Literal["month", "year"]
    state_code: str = Field(pattern=r"^\d{2}$")
    email: str = Field(min_length=3, max_length=254)
    legal_name: str | None = Field(default=None, max_length=200)
    gstin: str | None = Field(default=None, pattern=GSTIN)


class VerifyBody(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


class ChangeBody(BaseModel):
    plan_code: str
    interval: Literal["month", "year"]


def checkout_json(o: CheckoutOptions) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subscription_id": str(o.subscription_id),
        "status": o.status.value,
        "razorpay": {
            "key": o.key,
            "subscription_id": o.gateway_subscription_id,
            "name": o.name,
            "description": o.description,
            "prefill": {"email": o.email},
            "notes": {"billing_subscription_id": str(o.subscription_id)},
        },
    }
    if o.change_id is not None:
        body["change"] = {"id": str(o.change_id), "kind": o.kind}
    return body


@router.post("/checkout")
async def checkout(body: CheckoutBody, user: User, svc: Svc) -> JSONResponse:
    options = await svc.checkout(
        user,
        body.plan_code,
        Interval(body.interval),
        body.state_code,
        body.email,
        body.legal_name,
        body.gstin,
    )
    return JSONResponse(checkout_json(options), status_code=201)


@router.post("/checkout/verify")
async def verify(body: VerifyBody, user: User, svc: Svc) -> JSONResponse:
    result = await svc.verify_checkout(
        user, body.razorpay_payment_id, body.razorpay_subscription_id, body.razorpay_signature
    )
    return JSONResponse(
        {"subscription_id": str(result.subscription_id), "status": result.status.value}
    )


@router.post("/subscription/change")
async def change(body: ChangeBody, user: User, svc: Svc) -> JSONResponse:
    options = await svc.change_plan(user, body.plan_code, Interval(body.interval))
    return JSONResponse(checkout_json(options), status_code=201)


@router.post("/subscription/cancel")
async def cancel(user: User, svc: Svc) -> JSONResponse:
    result = await svc.cancel(user)
    return JSONResponse(
        {"subscription_id": str(result.subscription_id), "status": result.status.value}
    )


@router.post("/subscription/resume")
async def resume(user: User, svc: Svc) -> JSONResponse:
    return JSONResponse(checkout_json(await svc.resume(user)), status_code=201)
