"""Public routes: no auth header required."""

import hashlib
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from billing.adapters.db.uow import SqlUnitOfWork
from billing.adapters.ids import Uuid7Generator
from billing.app.records import PlanVersionRecord

router = APIRouter(prefix="/billing/v1")

INDIVIDUAL = ("free", "pro", "max")
_ids = Uuid7Generator()


def _plan_json(rec: PlanVersionRecord) -> dict[str, Any]:
    p = rec.plan
    prices = {}
    if p.price_month is not None:
        prices["month"] = int(p.price_month)
    if p.price_year is not None:
        prices["year"] = int(p.price_year)
    return {
        "code": p.plan_code,
        "version": p.version,
        "rank": p.rank,
        "prices": prices,
        "groups": sorted(g.value for g in p.allowed_groups),
        "extra_models": sorted(p.extra_models),
        "limits": asdict(p.limits),
    }


@router.get("/plans")
async def list_plans(request: Request) -> Response:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        return JSONResponse({"code": "UNAVAILABLE", "title": "database not configured"}, 503)
    payg = bool(getattr(request.app.state, "payg_enabled", False))
    allowed = {*INDIVIDUAL, *(["payg"] if payg else [])}  # organisation plans are never listed

    async with SqlUnitOfWork(engine, _ids) as uow:
        current = await uow.plans.all_current()
    shown = sorted(
        (r for r in current if r.plan.plan_code in allowed and r.plan.variant is None),
        key=lambda r: (r.plan.rank, r.plan.plan_code),
    )

    etag = '"' + hashlib.sha256("|".join(r.checksum for r in shown).encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    candidates = [
        t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")
    ]
    if etag in candidates or "*" in candidates:
        return Response(status_code=304, headers=headers)

    body = {
        "catalog_version": getattr(request.app.state, "catalog_version", ""),
        "currency": "INR",
        "gst_inclusive": True,
        "plans": [_plan_json(r) for r in shown],
    }
    return JSONResponse(body, headers=headers)
