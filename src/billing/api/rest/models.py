"""GET /billing/v1/models (public) and GET /billing/v1/me/models (the user's view)."""

import hashlib
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from billing.api.rest.billing import User
from billing.app.model_picker import ModelPicker

router = APIRouter(prefix="/billing/v1")


def _picker(request: Request) -> ModelPicker:
    picker: ModelPicker = request.app.state.picker
    return picker


@router.get("/models")
async def list_models(request: Request) -> Response:
    models = await _picker(request).all_models()
    body = json.dumps({"models": models}, separators=(",", ":"), sort_keys=True)
    etag = '"' + hashlib.sha256(body.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    candidates = [
        t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")
    ]
    if etag in candidates or "*" in candidates:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/me/models")
async def my_models(request: Request, user: User) -> JSONResponse:
    return JSONResponse(await _picker(request).for_user(user))
