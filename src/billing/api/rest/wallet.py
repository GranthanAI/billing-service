"""Pay as you Go wallet routes for the signed-in user (X-User-Id from api-gateway)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing.api.rest.billing import User
from billing.app.wallet import TopupOptions, WalletService
from billing.domain.catalog import OwnerType
from billing.domain.money import Paise

router = APIRouter(prefix="/billing/v1/wallet")


def _service(request: Request) -> WalletService:
    svc: WalletService = request.app.state.wallet
    return svc


Svc = Annotated[WalletService, Depends(_service)]


class TopupBody(BaseModel):
    pack_code: str
    state_code: str | None = Field(default=None, pattern=r"^[0-9]{2}$")
    email: str | None = None


class VerifyBody(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class SettingsBody(BaseModel):
    monthly_cap_paise: int | None = Field(default=None, ge=0)


def topup_json(o: TopupOptions) -> dict[str, object]:
    return {
        "order_id": o.order_id,
        "amount_paise": o.amount_paise,
        "razorpay": {
            "key": o.key,
            "order_id": o.order_id,
            "amount": o.amount_paise,
            "currency": "INR",
            "name": o.name,
            "description": o.description,
            "prefill": {"email": o.email} if o.email else {},
        },
    }


@router.get("")
async def get_wallet(user: User, svc: Svc) -> JSONResponse:
    return JSONResponse(jsonable_encoder({"wallet": await svc.view(user)}))


@router.get("/transactions")
async def transactions(
    user: User, svc: Svc, limit: Annotated[int, Query(le=200)] = 50
) -> JSONResponse:
    return JSONResponse(jsonable_encoder({"transactions": await svc.transactions(user, limit)}))


@router.post("/topups")
async def topup(body: TopupBody, user: User, svc: Svc) -> JSONResponse:
    options = await svc.start_topup(
        OwnerType.USER, user, pack_code=body.pack_code, email=body.email, state_code=body.state_code
    )
    return JSONResponse(topup_json(options), status_code=201)


@router.post("/topups/verify")
async def verify(body: VerifyBody, user: User, svc: Svc) -> JSONResponse:
    balance = await svc.verify(
        user, body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    )
    return JSONResponse({"balance_paise": balance})


@router.put("/settings")
async def settings(body: SettingsBody, user: User, svc: Svc) -> JSONResponse:
    await svc.set_cap(
        user, Paise(body.monthly_cap_paise) if body.monthly_cap_paise is not None else None
    )
    return JSONResponse({"monthly_cap_paise": body.monthly_cap_paise})
