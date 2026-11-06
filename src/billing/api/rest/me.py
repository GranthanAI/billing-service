"""The signed-in user's own billing views (X-User-Id from api-gateway)."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from billing.api.rest.billing import User
from billing.app.account_views import AccountViews
from billing.app.errors import NotFound

router = APIRouter(prefix="/billing/v1")


def _views(request: Request) -> AccountViews:
    views: AccountViews = request.app.state.views
    return views


Views = Annotated[AccountViews, Depends(_views)]


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
