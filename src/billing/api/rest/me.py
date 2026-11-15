"""The signed-in user's own billing views (X-User-Id from api-gateway)."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, RedirectResponse

from billing.api.rest.billing import User
from billing.app.account_views import AccountViews
from billing.app.billing_profile import BillingProfileService
from billing.app.errors import NotFound

router = APIRouter(prefix="/billing/v1")


def _views(request: Request) -> AccountViews:
    views: AccountViews = request.app.state.views
    return views


Views = Annotated[AccountViews, Depends(_views)]


class ProfileBody(BaseModel):
    billing_email: str = Field(max_length=254)
    state_code: str = Field(pattern=r"^\d{2}$")
    gstin: str | None = Field(default=None, max_length=15)
    legal_name: str | None = Field(default=None, max_length=200)


def _profile(request: Request) -> BillingProfileService:
    svc: BillingProfileService = request.app.state.profile
    return svc


Profile = Annotated[BillingProfileService, Depends(_profile)]


@router.get("/me/billing-profile")
async def get_billing_profile(user: User, svc: Profile) -> JSONResponse:
    return JSONResponse((await svc.get(user)).as_json(), headers={"Cache-Control": "no-store"})


@router.put("/me/billing-profile")
async def put_billing_profile(body: ProfileBody, user: User, svc: Profile) -> JSONResponse:
    """Applies to future invoices only; every invoice keeps the details it was issued with."""
    saved = await svc.update(user, body.billing_email, body.state_code, body.gstin, body.legal_name)
    return JSONResponse(saved.as_json())


@router.get("/me")
async def me(user: User, views: Views) -> JSONResponse:
    return JSONResponse(await views.me(user), headers={"Cache-Control": "no-store"})


@router.get("/me/usage")
async def my_usage(
    user: User,
    views: Views,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> JSONResponse:
    return JSONResponse(await views.usage(user, from_, to))


@router.get("/invoices")
async def my_invoices(user: User, views: Views) -> JSONResponse:
    return JSONResponse(await views.invoices(user), headers={"Cache-Control": "no-store"})


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: str, user: User, views: Views) -> RedirectResponse:
    try:
        parsed = UUID(invoice_id)
    except ValueError:
        raise NotFound("invoice not found") from None
    url = await views.pdf_url(user, parsed)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
