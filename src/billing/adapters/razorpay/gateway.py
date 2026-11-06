"""PaymentGateway over Razorpay's REST API (httpx; the official SDK is synchronous)."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog
from opentelemetry import trace

from billing.adapters.razorpay import mapping, signatures
from billing.app.errors import GatewayRejected, GatewayUnavailable
from billing.app.ports import (
    Clock,
    GatewayPayment,
    GatewayPlanSpec,
    GatewayRefund,
    GatewaySubscription,
    SettlementItem,
)
from billing.domain.money import Paise

log = structlog.get_logger()
tracer = trace.get_tracer("billing")

BASE_URL = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 10.0
ATTEMPTS = 4  # the first try plus 3 retries
PAGE = 100
MAX_PAGES = 20


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _rejection(resp: httpx.Response) -> GatewayRejected:
    err: Mapping[str, Any] = {}
    with contextlib.suppress(ValueError, AttributeError):
        err = resp.json().get("error", {})
    return GatewayRejected(
        str(err.get("code", f"HTTP_{resp.status_code}")),
        str(err.get("description", resp.text[:200])),
    )


class RazorpayGateway:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        clock: Clock,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._clock = clock
        self._sleep = sleep
        self._backoff = backoff_seconds
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(key_id, key_secret),
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- transport -----------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Retries network errors, 429 and 5xx with backoff; any other 4xx is GatewayRejected."""
        operation = f"razorpay.{method.lower()}_{path.strip('/').split('/')[0]}"
        with tracer.start_as_current_span(operation):
            return await self._request_with_retries(method, path, json, params)

    async def _request_with_retries(
        self, method: str, path: str, json: Any, params: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        last = "no attempt made"
        for attempt in range(ATTEMPTS):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                resp = await self._http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:  # timeouts, connection resets, DNS
                last = type(exc).__name__
                log.warning("razorpay_network_error", path=path, error=last, attempt=attempt)
                continue
            if _retryable(resp.status_code):
                last = f"HTTP {resp.status_code}"
                log.warning("razorpay_retryable", path=path, status=resp.status_code)
                continue
            if resp.status_code >= 400:
                raise _rejection(resp)
            body: dict[str, Any] = resp.json()
            return body
        raise GatewayUnavailable(f"{method} {path} failed after {ATTEMPTS} attempts: {last}")

    async def _list(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every item of a paginated collection (count/skip), newest first."""
        items: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            body = await self._request(
                "GET", path, params={**(params or {}), "count": PAGE, "skip": page * PAGE}
            )
            batch: list[dict[str, Any]] = body.get("items", [])
            items.extend(batch)
            if len(batch) < PAGE:
                break
        return items

    # --- plans ---------------------------------------------------------------------------

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        checksum, interval = spec.notes.get("checksum"), spec.notes.get("interval")
        for plan in await self._list("/plans"):
            notes = plan.get("notes") or {}
            if (
                isinstance(notes, dict)
                and notes.get("checksum") == checksum
                and notes.get("interval") == interval
            ):
                return str(plan["id"])
        body = await self._request(
            "POST",
            "/plans",
            json={
                "period": spec.period,
                "interval": spec.interval,
                "item": {
                    "name": spec.name,
                    "amount": int(spec.amount),
                    "currency": spec.currency,
                },
                "notes": dict(spec.notes),
            },
        )
        return str(body["id"])

    # --- subscriptions -------------------------------------------------------------------

    async def _find_subscription(
        self, plan_id: str, billing_ref: str
    ) -> GatewaySubscription | None:
        since = int((self._clock.now() - timedelta(days=2)).timestamp())
        body = await self._request(
            "GET", "/subscriptions", params={"plan_id": plan_id, "count": PAGE, "from": since}
        )
        for item in body.get("items", []):
            notes = item.get("notes") or {}
            if isinstance(notes, dict) and notes.get("billing_ref") == billing_ref:
                return mapping.subscription(item)
        return None

    async def create_subscription(
        self,
        plan_id: str,
        *,
        total_count: int,
        start_at: datetime | None,
        notes: Mapping[str, str],
        lookup_first: bool = False,
    ) -> GatewaySubscription:
        ref = notes.get("billing_ref")
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "total_count": total_count,
            "customer_notify": 1,
            "notes": dict(notes),
        }
        if start_at is not None:
            payload["start_at"] = int(start_at.timestamp())
        if ref and lookup_first and (found := await self._find_subscription(plan_id, ref)):
            return found
        last: GatewayUnavailable | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            if attempt and ref:  # a retry: the previous POST may have succeeded
                try:
                    if found := await self._find_subscription(plan_id, ref):
                        return found
                except GatewayUnavailable:
                    pass
            try:
                return mapping.subscription(await self._post_once("/subscriptions", payload))
            except GatewayUnavailable as exc:
                last = exc
        assert last is not None
        raise last

    async def _post_once(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One POST attempt (no inner retries), so create_subscription can look before retrying."""
        try:
            resp = await self._http.post(path, json=payload)
        except httpx.TransportError as exc:
            raise GatewayUnavailable(f"POST {path}: {type(exc).__name__}") from exc
        if _retryable(resp.status_code):
            raise GatewayUnavailable(f"POST {path}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise _rejection(resp)
        body: dict[str, Any] = resp.json()
        return body

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        return mapping.subscription(await self._request("GET", f"/subscriptions/{subscription_id}"))

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> GatewaySubscription:
        body = await self._request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            json={"cancel_at_cycle_end": 1 if at_cycle_end else 0},
        )
        return mapping.subscription(body)

    # --- payments and refunds ------------------------------------------------------------

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        return mapping.payment(await self._request("GET", f"/payments/{payment_id}"))

    async def list_subscription_payments(self, subscription_id: str) -> list[GatewayPayment]:
        out: list[GatewayPayment] = []
        for inv in await self._list("/invoices", {"subscription_id": subscription_id}):
            if inv.get("status") == "paid" and inv.get("payment_id"):
                pay = await self.fetch_payment(str(inv["payment_id"]))
                if pay.status == "captured":
                    out.append(pay)
        return sorted(out, key=lambda p: p.created_at, reverse=True)

    async def refund_payment(
        self, payment_id: str, amount: Paise, *, notes: Mapping[str, str]
    ) -> GatewayRefund:
        key = notes.get("refund_key")
        if key:
            for item in await self._list(f"/payments/{payment_id}/refunds"):
                existing = item.get("notes") or {}
                if isinstance(existing, dict) and existing.get("refund_key") == key:
                    return mapping.refund(item)
        body = await self._request(
            "POST",
            f"/payments/{payment_id}/refund",
            json={"amount": int(amount), "notes": dict(notes)},
        )
        return mapping.refund(body)

    async def fetch_refund(self, refund_id: str) -> GatewayRefund:
        return mapping.refund(await self._request("GET", f"/refunds/{refund_id}"))

    async def list_settlement_items(self, day: date) -> list[SettlementItem]:
        items = await self._list(
            "/settlements/recon/combined",
            {"year": day.year, "month": day.month, "day": day.day},
        )
        return [mapping.settlement_item(i) for i in items]

    async def list_payments(self, start: datetime, end: datetime) -> list[GatewayPayment]:
        raw = await self._list(
            "/payments", {"from": int(start.timestamp()), "to": int(end.timestamp()) - 1}
        )
        return [mapping.payment(p) for p in raw if p.get("status") == "captured"]

    # --- signatures ----------------------------------------------------------------------

    def verify_checkout_signature(
        self, payment_id: str, subscription_id: str, signature: str
    ) -> bool:
        return signatures.verify_checkout(self._key_secret, payment_id, subscription_id, signature)

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        return signatures.verify_webhook(self._webhook_secret, raw_body, signature)
