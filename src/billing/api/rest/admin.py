"""/admin/billing/v1: staff routes. billing_admin may write, support may only read."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from billing.adapters.catalog.catalog_io import raw_to_yaml
from billing.api.rest.idempotency import parse_user_id
from billing.app.admin import AdminService, Staff
from billing.app.catalog_admin import CatalogAdminService
from billing.app.errors import Forbidden, Unauthenticated
from billing.domain.catalog import ModelGroup, OrgVariant, OwnerType
from billing.domain.money import Paise

router = APIRouter(prefix="/admin/billing/v1")
ROLES = ("billing_admin", "support")


def _staff(request: Request) -> Staff:
    staff_id = parse_user_id(request.headers.get("X-User-Id"))
    role = request.headers.get("X-Staff-Role", "")
    if staff_id is None or role not in ROLES:
        raise Unauthenticated("X-User-Id and X-Staff-Role (billing_admin or support) are required")
    return Staff(staff_id, role)


def _writer(staff: Annotated[Staff, Depends(_staff)]) -> Staff:
    if not staff.can_write:
        raise Forbidden("this change needs the billing_admin role")
    return staff


Reader = Annotated[Staff, Depends(_staff)]
Writer = Annotated[Staff, Depends(_writer)]


def _svc(request: Request) -> AdminService:
    svc: AdminService = request.app.state.admin
    return svc


Svc = Annotated[AdminService, Depends(_svc)]


def _catalog_svc(request: Request) -> CatalogAdminService:
    svc: CatalogAdminService | None = getattr(request.app.state, "catalog_admin", None)
    if svc is None:
        raise Unauthenticated("the catalog API is not configured on this instance")
    return svc


CatalogSvc = Annotated[CatalogAdminService, Depends(_catalog_svc)]


def _json(data: Any, status: int = 200) -> JSONResponse:
    from fastapi.encoders import jsonable_encoder

    return JSONResponse(jsonable_encoder(data), status_code=status)


class Reason(BaseModel):
    reason: str = Field(default="", max_length=1000)


class OverrideBody(Reason):
    owner_type: Literal["user", "org"]
    owner_id: UUID
    plan_code: str
    expires_at: datetime
    starts_at: datetime | None = None
    variant: Literal["college", "company"] | None = None


class RefundBody(Reason):
    payment_id: UUID
    amount_paise: int


class SettlementBody(Reason):
    payment_id: UUID
    fee_paise: int = Field(ge=0)
    fee_gst_paise: int = Field(ge=0)


class ModelPriceBody(Reason):
    model: str
    provider: str
    group: ModelGroup
    input_usd_per_m: Decimal
    cached_input_usd_per_m: Decimal | None = None
    output_usd_per_m: Decimal
    effective_from: datetime


class DraftBody(BaseModel):
    catalog: dict[str, Any]  # the plans.yaml schema, as JSON
    note: str = Field(default="", max_length=1000)


class FxRateBody(Reason):
    effective_from: date
    usd_inr: Decimal
    source: str = "admin"


# --- reads ------------------------------------------------------------------------------------


@router.get("/accounts")
async def search_accounts(_: Reader, svc: Svc, q: str = "") -> JSONResponse:
    return _json({"accounts": await svc.search(q)})


@router.get("/accounts/{account_id}")
async def account(account_id: UUID, _: Reader, svc: Svc) -> JSONResponse:
    return _json(await svc.account(account_id))


@router.get("/costs")
async def costs(
    _: Reader,
    svc: Svc,
    from_: Annotated[date, Query(alias="from")],
    to: date,
    by: str = "plan",
) -> JSONResponse:
    return _json({"by": by, "from": from_, "to": to, "rows": await svc.costs(from_, to, by)})


@router.get("/reconciliation")
async def reconciliation(
    _: Reader, svc: Svc, date_: Annotated[date, Query(alias="date")]
) -> JSONResponse:
    return _json({"date": date_, "checks": await svc.reconciliation(date_)})


# --- writes -----------------------------------------------------------------------------------


@router.post("/overrides")
async def create_override(body: OverrideBody, staff: Writer, svc: Svc) -> JSONResponse:
    record = await svc.create_override(
        staff, OwnerType(body.owner_type), body.owner_id, body.plan_code, body.reason,
        body.expires_at, body.starts_at,
        OrgVariant(body.variant) if body.variant else None,
    )  # fmt: skip
    return _json({"id": record.id, "expires_at": record.expires_at}, 201)


@router.post("/overrides/{override_id}/end")
async def end_override(override_id: UUID, body: Reason, staff: Writer, svc: Svc) -> JSONResponse:
    await svc.end_override(staff, override_id, body.reason)
    return _json({"ended": True})


@router.post("/refunds")
async def refund(body: RefundBody, staff: Writer, svc: Svc) -> JSONResponse:
    key = await svc.refund(staff, body.payment_id, Paise(body.amount_paise), body.reason)
    return _json({"refund_key": key, "status": "queued"}, 201)


@router.post("/accounts/{account_id}/block")
async def block(account_id: UUID, body: Reason, staff: Writer, svc: Svc) -> JSONResponse:
    await svc.block(staff, account_id, body.reason, blocked=True)
    return _json({"blocked": True})


@router.post("/accounts/{account_id}/unblock")
async def unblock(account_id: UUID, body: Reason, staff: Writer, svc: Svc) -> JSONResponse:
    await svc.block(staff, account_id, body.reason, blocked=False)
    return _json({"blocked": False})


@router.put("/model-prices")
async def put_model_price(body: ModelPriceBody, staff: Writer, svc: Svc) -> JSONResponse:
    await svc.put_model_price(
        staff, model=body.model, provider=body.provider, group=body.group,
        input_usd_per_m=body.input_usd_per_m, cached_input_usd_per_m=body.cached_input_usd_per_m,
        output_usd_per_m=body.output_usd_per_m, effective_from=body.effective_from,
        reason=body.reason,
    )  # fmt: skip
    return _json({"model": body.model, "effective_from": body.effective_from}, 201)


@router.put("/fx-rates")
async def put_fx_rate(body: FxRateBody, staff: Writer, svc: Svc) -> JSONResponse:
    await svc.put_fx_rate(staff, body.effective_from, body.usd_inr, body.source, body.reason)
    return _json({"effective_from": body.effective_from, "usd_inr": body.usd_inr}, 201)


@router.post("/catalog/apply-limits/{audit_id}")
async def apply_limits(audit_id: int, body: Reason, staff: Writer, svc: Svc) -> JSONResponse:
    moved = await svc.apply_limits(staff, audit_id, body.reason)
    return _json({"subscriptions_moved": moved})


# --- the plan catalog (drafts, publish, export) -------------------------------------------------


@router.get("/catalog")
async def get_catalog(staff: Reader, svc: CatalogSvc) -> JSONResponse:
    return _json(await svc.get_catalog())


@router.get("/catalog/export.yaml")
async def export_catalog(staff: Reader, svc: CatalogSvc) -> PlainTextResponse:
    return PlainTextResponse(
        raw_to_yaml(await svc.export_raw()),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="plans.yaml"'},
    )


@router.post("/catalog/drafts")
async def create_draft(body: DraftBody, staff: Writer, svc: CatalogSvc) -> JSONResponse:
    return _json(await svc.create_draft(staff, body.catalog, body.note), 201)


@router.get("/catalog/drafts")
async def list_drafts(staff: Reader, svc: CatalogSvc) -> JSONResponse:
    return _json({"drafts": await svc.list_drafts()})


@router.get("/catalog/drafts/{draft_id}")
async def get_draft(draft_id: UUID, staff: Reader, svc: CatalogSvc) -> JSONResponse:
    return _json(await svc.get_draft(draft_id))


@router.post("/catalog/drafts/{draft_id}/publish")
async def publish_draft(
    draft_id: UUID, body: Reason, staff: Writer, svc: CatalogSvc
) -> JSONResponse:
    return _json(await svc.publish(staff, draft_id, body.reason))


@router.post("/settlements")
async def post_settlement(body: SettlementBody, staff: Writer, svc: Svc) -> JSONResponse:
    posted = await svc.post_settlement(
        staff, body.payment_id, Paise(body.fee_paise), Paise(body.fee_gst_paise), body.reason
    )
    return _json({"posted": posted}, 201 if posted else 200)
