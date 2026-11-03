"""POST /billing/v1/webhooks/razorpay: verify, store, acknowledge. No business logic."""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from billing.api.rest.errors import problem
from billing.app.ports import PaymentGateway, UnitOfWork
from billing.observability import WEBHOOK_REJECTED, WEBHOOKS_RECEIVED

router = APIRouter(prefix="/billing/v1")
MAX_BODY = 1_000_000


async def _read_capped(request: Request) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> Response:
    raw = await _read_capped(request)  # raw bytes, before any parsing
    if raw is None:
        return problem("VALIDATION_FAILED", "The request is too large", "limit is 1 MB", 413)
    gateway: PaymentGateway = request.app.state.gateway
    if not gateway.verify_webhook_signature(raw, request.headers.get("X-Razorpay-Signature", "")):
        WEBHOOK_REJECTED.inc()
        WEBHOOKS_RECEIVED.labels(event_type="unknown", signature_ok="false").inc()
        return problem("INVALID_SIGNATURE", "The signature does not match", "", 400)
    try:
        payload: dict[str, Any] = json.loads(raw)
        event_type = str(payload["event"])
    except (ValueError, KeyError, TypeError):
        return problem("VALIDATION_FAILED", "The request is not valid", "not a Razorpay event", 400)
    event_id = request.headers.get("x-razorpay-event-id") or hashlib.sha256(raw).hexdigest()
    uow_factory: Callable[[], UnitOfWork] = request.app.state.uow_factory
    async with uow_factory() as uow:
        await uow.inbox.store("razorpay", event_id, event_type, payload)
    WEBHOOKS_RECEIVED.labels(event_type=event_type, signature_ok="true").inc()
    return JSONResponse({"status": "ok"})
