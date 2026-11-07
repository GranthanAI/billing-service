"""Organisation billing routes.

/admin/billing/v1/orgs/...: staff (billing_admin) subscribe an organisation, change its seats,
mark an invoice paid (bank transfer) or make its payment link.
/internal/orgs/{org_id}/usage: org-service reads its members' usage with a service token.
"""

import csv
import hmac
import io
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from billing.api.rest.admin import Writer
from billing.app.admin import need_reason
from billing.app.errors import NotFound, Unauthenticated, ValidationFailed
from billing.app.org_billing import IssuedInvoice, OrgBilling
from billing.app.org_topups import OrgTopups
from billing.domain.catalog import OrgVariant
from billing.domain.money import Paise

admin_router = APIRouter(prefix="/admin/billing/v1")
internal_router = APIRouter(prefix="/internal")


def _billing(request: Request) -> OrgBilling:
    svc: OrgBilling = request.app.state.org_billing
    return svc


Svc = Annotated[OrgBilling, Depends(_billing)]


def _json(data: object, status: int = 200) -> JSONResponse:
    return JSONResponse(jsonable_encoder(data), status_code=status)


CSV_COLUMNS = ("user_id", "day", "model_group", "messages", "billable_units")


def _csv(rows: list[dict[str, Any]]) -> Response:
    """One line per member, day and model group, ready for a spreadsheet."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8")


def _issued(i: IssuedInvoice) -> dict[str, object]:
    return {
        "subscription_id": i.subscription_id,
        "invoice_id": i.invoice_id,
        "invoice_number": i.number,
        "total_paise": i.total_paise,
        "payment_link_url": i.payment_link_url,
    }


class SubscribeBody(BaseModel):
    plan_code: str
    variant: OrgVariant
    seats: int = Field(ge=1)
    reason: str = ""
    start: datetime | None = None
    payment_link: bool = True


class SeatsBody(BaseModel):
    seats: int = Field(ge=1)
    reason: str = ""


class MarkPaidBody(BaseModel):
    reference: str = Field(min_length=1, max_length=200)
    reason: str = ""
    paid_at: datetime | None = None
    amount_paise: int | None = None


@admin_router.post("/orgs/{org_id}/subscriptions")
async def subscribe(org_id: UUID, body: SubscribeBody, staff: Writer, svc: Svc, request: Request):  # type: ignore[no-untyped-def]
    reason = need_reason(body.reason)
    org = await request.app.state.org_directory.org(org_id)
    if org is None:
        raise NotFound("org-service does not know this organisation")
    issued = await svc.subscribe(
        org, body.plan_code, body.variant, body.seats,
        start=body.start, with_link=body.payment_link, actor=staff.id, reason=reason,
    )  # fmt: skip
    return _json(_issued(issued), 201)


@admin_router.post("/orgs/{org_id}/seats")
async def change_seats(org_id: UUID, body: SeatsBody, staff: Writer, svc: Svc):  # type: ignore[no-untyped-def]
    outcome, issued = await svc.change_seats(
        org_id, body.seats, actor=staff.id, reason=need_reason(body.reason)
    )
    return _json({"outcome": outcome, "invoice": _issued(issued) if issued else None})


@admin_router.post("/invoices/{invoice_id}/mark-paid")
async def mark_paid(invoice_id: UUID, body: MarkPaidBody, staff: Writer, svc: Svc):  # type: ignore[no-untyped-def]
    done = await svc.mark_paid(
        invoice_id,
        reference=body.reference,
        paid_at=body.paid_at,
        amount=Paise(body.amount_paise) if body.amount_paise is not None else None,
        actor=staff.id,
        reason=need_reason(body.reason),
    )
    return _json({"paid": True, "changed": done})


@admin_router.post("/invoices/{invoice_id}/payment-link")
async def payment_link(invoice_id: UUID, staff: Writer, svc: Svc):  # type: ignore[no-untyped-def]
    return _json({"payment_link_url": await svc.create_link(invoice_id)})


def _topups(request: Request) -> OrgTopups:
    svc: OrgTopups = request.app.state.org_topups
    svc.require_enabled()  # PAYG_ENABLED=false: these routes do not exist
    return svc


Topups = Annotated[OrgTopups, Depends(_topups)]


class TopupBody(BaseModel):
    amount_paise: int = Field(gt=0)  # GST included, as invoiced
    reason: str = ""


class TopupMarkPaidBody(BaseModel):
    reference: str = Field(min_length=1, max_length=200)
    reason: str = ""


class ReasonBody(BaseModel):
    reason: str = ""


@admin_router.post("/orgs/{org_id}/wallet/topups")
async def create_topup(org_id: UUID, body: TopupBody, staff: Writer, svc: Topups, request: Request):  # type: ignore[no-untyped-def]
    reason = need_reason(body.reason)
    org = await request.app.state.org_directory.org(org_id)
    if org is None:
        raise NotFound("org-service does not know this organisation")
    return _json(
        await svc.create(org, Paise(body.amount_paise), actor=staff.id, reason=reason), 201
    )


@admin_router.post("/orgs/{org_id}/wallet/topups/{request_id}/mark-paid")
async def mark_topup_paid(
    org_id: UUID, request_id: UUID, body: TopupMarkPaidBody, staff: Writer, svc: Topups
) -> JSONResponse:
    return _json(
        await svc.mark_paid(
            org_id, request_id, reference=body.reference, actor=staff.id,
            reason=need_reason(body.reason),
        )
    )  # fmt: skip


@admin_router.post("/orgs/{org_id}/wallet/topups/{request_id}/cancel")
async def cancel_topup(
    org_id: UUID, request_id: UUID, body: ReasonBody, staff: Writer, svc: Topups
) -> JSONResponse:
    return _json(
        await svc.cancel(org_id, request_id, actor=staff.id, reason=need_reason(body.reason))
    )


def _service(request: Request, authorization: Annotated[str | None, Header()] = None) -> OrgBilling:
    token: str = getattr(request.app.state, "org_service_token", "")
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(presented.encode(), token.encode()):
        raise Unauthenticated("a service token is required")
    return _billing(request)


@internal_router.get("/orgs/{org_id}/usage")
async def org_usage(  # type: ignore[no-untyped-def]
    org_id: UUID,
    request: Request,
    svc: Annotated[OrgBilling, Depends(_service)],
    from_: Annotated[date, Query(alias="from")],
    to: date,
    format: Literal["json", "csv"] = "json",
):
    if to < from_:
        raise ValidationFailed("'to' is before 'from'")
    members = await request.app.state.org_directory.members(org_id)
    rows = await svc.usage_report(members, from_, to)
    if format == "csv":
        return _csv(rows)
    return _json(
        {
            "org_id": org_id,
            "from": from_,
            "to": to,
            "rows": rows,
        }
    )
