"""Payment-simulator routes: the simulated checkout page and the control API.

Mounted ONLY when PAYMENT_SIMULATION=true (`install_sim_routes`); without it these paths are 404.
The two checkout routes are public but need the signed token from the checkout response; the
control routes need X-Gateway-Secret like everything else, plus X-Staff-Role: billing_admin.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import jinja2
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from billing.adapters.gateway_sim import SimulatorGateway
from billing.app.errors import Forbidden, GatewayRejected, NotFound, Unauthenticated

router = APIRouter(prefix="/billing/v1/sim")
CHECKOUT_PREFIX = "/billing/v1/sim/checkout/"
TEMPLATES = Path(__file__).resolve().parents[2] / "adapters" / "templates"
_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES),
    autoescape=jinja2.select_autoescape(["html"]),
    undefined=jinja2.StrictUndefined,
)


def _gateway(request: Request) -> SimulatorGateway:
    gateway = request.app.state.gateway
    if not isinstance(gateway, SimulatorGateway):
        raise NotFound("the payment simulator is not running")
    return gateway


Gw = Annotated[SimulatorGateway, Depends(_gateway)]


def _admin(request: Request) -> None:
    if request.headers.get("X-Staff-Role") != "billing_admin":
        raise Unauthenticated("X-Staff-Role: billing_admin is required")


Admin = Depends(_admin)


async def _checked(gw: SimulatorGateway, gateway_id: str, token: str | None) -> dict[str, Any]:
    if not token or not gw.check_token(gateway_id, token):
        raise Forbidden("this checkout link is invalid or has expired")
    try:
        return await gw.checkout_info(gateway_id)
    except GatewayRejected:
        raise NotFound("unknown checkout") from None


@router.get("/checkout/{gateway_id}", response_class=HTMLResponse)
async def checkout_page(gateway_id: str, request: Request, gw: Gw, t: str | None = None) -> Any:
    info = await _checked(gw, gateway_id, t)
    html = _ENV.get_template("sim_checkout.html").render(
        gateway_id=gateway_id,
        label=info["label"],
        amount_rupees=f"{info['amount'] / 100:,.2f}",
        methods=info["methods"],
        origin=getattr(request.app.state, "frontend_origin", "*"),
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class PayBody(BaseModel):
    method: Literal["card", "upi", "emandate"]
    card_number: str = ""
    vpa: str = ""
    otp: str = ""


@router.post("/checkout/{gateway_id}/pay")
async def checkout_pay(
    gateway_id: str, body: PayBody, gw: Gw, t: str | None = None
) -> JSONResponse:
    await _checked(gw, gateway_id, t)
    try:
        result = await gw.pay(
            gateway_id,
            method=body.method,
            card_number=body.card_number,
            vpa=body.vpa,
            otp=body.otp,
        )
    except GatewayRejected as exc:
        return JSONResponse({"error": exc.description}, status_code=409)
    if not result.ok:
        return JSONResponse({"error": result.error, "payment_id": result.payment_id}, 402)
    return JSONResponse(dict(result.handler or {}))  # exactly what Razorpay's handler gets


class OutcomeBody(BaseModel):
    outcome: Literal["success", "fail", "halt"]


def _reject(exc: GatewayRejected) -> NotFound:
    return NotFound(exc.description)


@router.post("/subscriptions/{sub_id}/next-charge", dependencies=[Admin])
async def next_charge(sub_id: str, body: OutcomeBody, gw: Gw) -> JSONResponse:
    try:
        sub = await gw.set_next_outcome(sub_id, body.outcome)
    except GatewayRejected as exc:
        raise _reject(exc) from exc
    return JSONResponse(jsonable_encoder({"id": sub.id, "status": sub.status, **body.model_dump()}))


@router.post("/subscriptions/{sub_id}/renew-now", dependencies=[Admin])
async def renew_now(sub_id: str, gw: Gw) -> JSONResponse:
    try:
        status = await gw.renew(sub_id, force=True)
    except GatewayRejected as exc:
        raise NotFound(exc.description) from exc
    return JSONResponse({"id": sub_id, "status": status})


@router.post("/subscriptions/{sub_id}/customer-cancel", dependencies=[Admin])
async def customer_cancel(sub_id: str, gw: Gw) -> JSONResponse:
    try:
        sub = await gw.customer_cancel(sub_id)
    except GatewayRejected as exc:
        raise NotFound(exc.description) from exc
    return JSONResponse({"id": sub.id, "status": sub.status})


@router.post("/refunds/{refund_id}/fail", dependencies=[Admin])
async def fail_refund(refund_id: str, gw: Gw) -> JSONResponse:
    try:
        refund = await gw.fail_refund(refund_id)
    except GatewayRejected as exc:
        raise NotFound(exc.description) from exc
    return JSONResponse({"id": refund.id, "status": refund.status})


@router.post("/settle-now", dependencies=[Admin])
async def settle_now(gw: Gw) -> JSONResponse:
    return JSONResponse({"settlement_id": await gw.settle(everything=True)})


@router.get("/state", dependencies=[Admin])
async def state(user_id: str, gw: Gw) -> JSONResponse:
    return JSONResponse(jsonable_encoder(await gw.state(user_id)))


def install_sim_routes(app: FastAPI) -> None:
    """Mount the simulator routes and open the checkout page's token-protected paths."""
    app.state.simulation = True
    if not any(getattr(r, "path", "").startswith(CHECKOUT_PREFIX) for r in app.routes):
        app.include_router(router)


def remove_sim_routes(app: FastAPI) -> None:
    """Undo `install_sim_routes` (tests only)."""
    app.state.simulation = False
    app.router.routes[:] = [
        r for r in app.routes if not getattr(r, "path", "").startswith("/billing/v1/sim/")
    ]
