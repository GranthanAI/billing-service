"""SimulatorGateway: a persistent, Razorpay-shaped PaymentGateway in Postgres (schema billing_sim).

It implements the same port as RazorpayGateway and returns the same DTOs, so every billing code
path (checkout, verify, webhooks, poll, reconcile, settlements) runs unchanged. Money only ever
moves through the methods below; every state change queues its Razorpay webhook in
`sim_webhook_outbox` in the same transaction. Delivery, renewals, refunds and settlements are driven
by the `sim.*` jobs (jobs/handlers/sim.py). Never used in production (settings refuse it).
"""

import hmac
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from billing.adapters import sim_events as ev
from billing.adapters.gateway_fake import add_months
from billing.app.errors import GatewayRejected
from billing.app.ports import (
    Clock,
    GatewayOrder,
    GatewayPayment,
    GatewayPaymentLink,
    GatewayPlanSpec,
    GatewayRefund,
    GatewaySubscription,
    SettlementItem,
)
from billing.domain.calendar import IST
from billing.domain.money import Paise

log = structlog.get_logger()

GATEWAY_NAME = "simulator"
UPI_LIMIT = 15_000_00  # paise: UPI is not offered for a single debit at or above this
MAX_RETRIES = 3
AUTH_AMOUNT = 500  # the refundable Rs 5 authorisation of a mandate that starts later
PRE_DEBIT_NOTICE = timedelta(hours=24)
SETTLE_AFTER = timedelta(days=2)
DELIVERY_ATTEMPTS = 10
CARD_OK, CARD_FAIL = "4111111111111111", "4000000000000002"
THIRTY_DAYS = 30 * 86_400
NOT_CANCELLABLE = ("cancelled", "completed", "expired")
Row = dict[str, Any]
Post = Callable[[bytes, Mapping[str, str]], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class SimConfig:
    month_seconds: int = 0  # 0: real calendar months
    retry_interval: int = 86_400
    refund_delay: int = 10
    webhook_url: str = "http://localhost:8080/billing/v1/webhooks/razorpay"
    webhook_delay: int = 1
    duplicate_pct: int = 0
    drop_pct: int = 0
    reorder: bool = False
    checkout_path: str = "/api/v1/billing/sim/checkout"

    @property
    def scale(self) -> float:
        """Compressed-time factor: 1.0 for real time, month_seconds / 30 days otherwise."""
        return self.month_seconds / THIRTY_DAYS if self.month_seconds > 0 else 1.0


@dataclass(frozen=True, slots=True)
class PayResult:
    ok: bool
    payment_id: str
    error: str | None = None
    handler: Mapping[str, str] | None = None  # what Razorpay Checkout hands its `handler`


def _reject(description: str = "The id provided does not exist") -> GatewayRejected:
    return GatewayRejected("BAD_REQUEST_ERROR", description)


def _notes(value: Any) -> Mapping[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _sub_dto(r: Row) -> GatewaySubscription:
    return GatewaySubscription(
        id=r["id"],
        plan_id=r["plan_id"],
        status=r["status"],
        current_start=r["current_start"],
        current_end=r["current_end"],
        charge_at=r["charge_at"],
        start_at=r["start_at"],
        paid_count=int(r["paid_count"]),
        notes=_notes(r["notes"]),
        short_url=r["short_url"],
    )


def _pay_dto(r: Row) -> GatewayPayment:
    return GatewayPayment(
        id=r["id"],
        amount=Paise(int(r["amount"])),
        currency=r["currency"],
        status=r["status"],
        method=r["method"],
        fee=Paise(int(r["fee"])) if r["fee"] is not None else None,
        tax=Paise(int(r["tax"])) if r["tax"] is not None else None,
        order_id=r["order_id"],
        invoice_id=r["invoice_id"],
        subscription_id=r["subscription_id"],
        created_at=r["created_at"],
        notes=_notes(r["notes"]),
        raw=ev.payment_entity(r),
        gateway=GATEWAY_NAME,
    )


def _refund_dto(r: Row) -> GatewayRefund:
    return GatewayRefund(
        r["id"], r["payment_id"], Paise(int(r["amount"])), r["status"], _notes(r["notes"])
    )


def _order_dto(r: Row) -> GatewayOrder:
    return GatewayOrder(
        r["id"], Paise(int(r["amount"])), r["status"], r["receipt"], _notes(r["notes"])
    )


def _link_dto(r: Row) -> GatewayPaymentLink:
    return GatewayPaymentLink(
        r["id"], r["short_url"], r["status"], r["reference_id"], Paise(int(r["amount"]))
    )


def _j(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}))


class SimulatorGateway:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        clock: Clock,
        config: SimConfig | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self.key_id = key_id
        self.key_secret = key_secret
        # a rotation list may be comma-joined; the first secret signs, all of them verify
        self.webhook_secrets = webhook_secret
        self._sign_secret = webhook_secret.split(",")[0]
        self._clock = clock
        self.config = config or SimConfig()
        self._rng = rng or random.Random()
        self._http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # --- plumbing ------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock.now()

    async def _one(self, conn: AsyncConnection, sql: str, **p: Any) -> Row | None:
        r = (await conn.execute(text(sql), p)).mappings().first()
        return dict(r) if r is not None else None

    async def _all(self, conn: AsyncConnection, sql: str, **p: Any) -> list[Row]:
        return [dict(r) for r in (await conn.execute(text(sql), p)).mappings().all()]

    async def _run(self, conn: AsyncConnection, sql: str, **p: Any) -> int:
        return int((await conn.execute(text(sql), p)).rowcount)

    async def _get(self, conn: AsyncConnection, table: str, row_id: str, lock: bool = False) -> Row:
        row = await self._one(
            conn,
            f"SELECT * FROM billing_sim.{table} WHERE id = :i" + (" FOR UPDATE" if lock else ""),
            i=row_id,
        )
        if row is None:
            raise _reject()
        return row

    def period_end(self, plan: Row, start: datetime) -> datetime:
        months = (12 if plan["period"] == "yearly" else 1) * int(plan["interval"] or 1)
        if self.config.month_seconds > 0:
            return start + timedelta(seconds=self.config.month_seconds * months)
        return add_months(start, months)

    def _retry_delta(self) -> timedelta:
        return timedelta(seconds=max(1.0, self.config.retry_interval * self.config.scale))

    def checkout_url(self, gateway_id: str) -> str:
        return f"{self.config.checkout_path}/{gateway_id}"

    # --- webhooks ------------------------------------------------------------------------

    async def _emit(self, conn: AsyncConnection, event: str, **entities: Mapping[str, Any]) -> None:
        """Queue the signed webhook for `event` (same transaction as the state change)."""
        now = self._now()
        body = ev.encode(ev.build_body(event, now, **entities))
        signature = ev.sign(self._sign_secret, body)
        event_id = ev.new_event_id()
        cfg = self.config
        dropped = self._rng.random() * 100 < cfg.drop_pct
        due = now + timedelta(seconds=cfg.webhook_delay)
        copies = [(due, "dropped" if dropped else "pending")]
        if not dropped and self._rng.random() * 100 < cfg.duplicate_pct:
            copies.append((due + timedelta(seconds=1), "pending"))
        for deliver_after, status in copies:
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_webhook_outbox "
                "(event_id, event, body, signature, status, deliver_after, created_at) "
                "VALUES (:e, :ev, :b, :s, :st, :d, :c)",
                e=event_id, ev=event, b=body, s=signature, st=status, d=deliver_after, c=now,
            )  # fmt: skip

    async def deliver_due(self, post: Post | None = None, limit: int = 50) -> int:
        """POST due webhooks (the exact signed bytes) to SIM_WEBHOOK_URL. Returns how many were
        delivered. A failure retries with backoff; after DELIVERY_ATTEMPTS the row is `failed`."""
        send = post or self._post_http
        now = self._now()
        async with self._engine.begin() as conn:
            rows = await self._all(
                conn,
                "UPDATE billing_sim.sim_webhook_outbox SET attempts = attempts + 1, "
                "deliver_after = :now + make_interval(secs => least(2 * power(2, attempts), 300)) "
                "WHERE id IN (SELECT id FROM billing_sim.sim_webhook_outbox "
                "WHERE status = 'pending' AND deliver_after <= :now "
                "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED) "
                "RETURNING id, event_id, body, signature, attempts",
                now=now, n=limit,
            )  # fmt: skip
        rows.sort(key=lambda r: r["id"], reverse=self.config.reorder)
        delivered = 0
        for r in rows:
            headers = {
                "Content-Type": "application/json",
                "X-Razorpay-Signature": r["signature"],
                "X-Razorpay-Event-Id": r["event_id"],
            }
            error: str | None = None
            try:
                status = await send(bytes(r["body"]), headers)
                if status >= 300:
                    error = f"HTTP {status}"
            except Exception as exc:  # network errors are retried like a non-2xx
                error = type(exc).__name__
            async with self._engine.begin() as conn:
                if error is None:
                    delivered += 1
                    await self._run(
                        conn,
                        "UPDATE billing_sim.sim_webhook_outbox SET status = 'delivered', "
                        "delivered_at = :n WHERE id = :i",
                        n=self._now(), i=r["id"],
                    )  # fmt: skip
                else:
                    failed = r["attempts"] >= DELIVERY_ATTEMPTS
                    await self._run(
                        conn,
                        "UPDATE billing_sim.sim_webhook_outbox SET last_error = :e, "
                        "status = CASE WHEN :f THEN 'failed' ELSE status END WHERE id = :i",
                        e=error, f=failed, i=r["id"],
                    )  # fmt: skip
        return delivered

    async def _post_http(self, body: bytes, headers: Mapping[str, str]) -> int:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        resp = await self._http.post(self.config.webhook_url, content=body, headers=dict(headers))
        return resp.status_code

    # --- plans and subscriptions ---------------------------------------------------------

    async def ensure_plan(self, spec: GatewayPlanSpec) -> str:
        checksum, interval = spec.notes.get("checksum"), spec.notes.get("interval")
        async with self._engine.begin() as conn:
            found = await self._one(
                conn,
                "SELECT id FROM billing_sim.sim_plans WHERE notes->>'checksum' IS NOT DISTINCT "
                "FROM CAST(:c AS text) AND notes->>'interval' IS NOT DISTINCT FROM "
                "CAST(:i AS text) LIMIT 1",
                c=checksum, i=interval,
            )  # fmt: skip
            if found is not None:
                return str(found["id"])
            plan_id = ev.new_id("plan")
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_plans (id, period, interval, amount, currency, name, "
                "notes, created_at) VALUES (:id, :p, :iv, :a, :cur, :n, CAST(:notes AS jsonb), :c)",
                id=plan_id, p=spec.period, iv=spec.interval, a=int(spec.amount),
                cur=spec.currency, n=spec.name, notes=_j(spec.notes), c=self._now(),
            )  # fmt: skip
            return plan_id

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
        async with self._engine.begin() as conn:
            await self._get(conn, "sim_plans", plan_id)
            if ref:
                found = await self._one(
                    conn,
                    "SELECT * FROM billing_sim.sim_subscriptions "
                    "WHERE plan_id = :p AND billing_ref = :r",
                    p=plan_id, r=ref,
                )  # fmt: skip
                if found is not None:
                    return _sub_dto(found)
            sub_id = ev.new_id("sub")
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_subscriptions (id, plan_id, status, charge_at, "
                "start_at, total_count, notes, billing_ref, short_url, created_at) VALUES "
                "(:id, :p, 'created', :s, :s, :t, CAST(:n AS jsonb), :r, :u, :c)",
                id=sub_id, p=plan_id, s=start_at, t=total_count, n=_j(notes), r=ref,
                u=self.checkout_url(sub_id), c=self._now(),
            )  # fmt: skip
            return _sub_dto(await self._get(conn, "sim_subscriptions", sub_id))

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        async with self._engine.connect() as conn:
            return _sub_dto(await self._get(conn, "sim_subscriptions", subscription_id))

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> GatewaySubscription:
        async with self._engine.begin() as conn:
            sub = await self._get(conn, "sim_subscriptions", subscription_id, lock=True)
            if sub["status"] in NOT_CANCELLABLE:
                raise _reject(f"Subscription is not cancellable in {sub['status']} status")
            if at_cycle_end:
                await self._run(
                    conn,
                    "UPDATE billing_sim.sim_subscriptions SET cancel_at_cycle_end = true "
                    "WHERE id = :i",
                    i=subscription_id,
                )
            else:
                await self._end_cancelled(conn, sub)
            return _sub_dto(await self._get(conn, "sim_subscriptions", subscription_id))

    async def _end_cancelled(self, conn: AsyncConnection, sub: Row) -> None:
        await self._run(
            conn,
            "UPDATE billing_sim.sim_subscriptions SET status = 'cancelled', charge_at = NULL, "
            "next_retry_at = NULL, ended_at = :n WHERE id = :i",
            n=self._now(), i=sub["id"],
        )  # fmt: skip
        await self._emit_sub(conn, "subscription.cancelled", sub["id"])

    async def _emit_sub(self, conn: AsyncConnection, event: str, sub_id: str, **more: Any) -> None:
        sub = await self._get(conn, "sim_subscriptions", sub_id)
        await self._emit(conn, event, subscription=ev.subscription_entity(sub), **more)

    # --- payments, refunds ---------------------------------------------------------------

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        async with self._engine.connect() as conn:
            return _pay_dto(await self._get(conn, "sim_payments", payment_id))

    async def list_subscription_payments(self, subscription_id: str) -> list[GatewayPayment]:
        async with self._engine.connect() as conn:
            rows = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_payments WHERE subscription_id = :s "
                "AND status = 'captured' ORDER BY created_at DESC, id DESC",
                s=subscription_id,
            )
        return [_pay_dto(r) for r in rows]

    async def list_payments(self, start: datetime, end: datetime) -> list[GatewayPayment]:
        async with self._engine.connect() as conn:
            rows = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_payments WHERE status = 'captured' "
                "AND created_at >= :s AND created_at < :e ORDER BY created_at, id",
                s=start, e=end,
            )  # fmt: skip
        return [_pay_dto(r) for r in rows]

    async def refund_payment(
        self, payment_id: str, amount: Paise, *, notes: Mapping[str, str]
    ) -> GatewayRefund:
        key = notes.get("refund_key")
        async with self._engine.begin() as conn:
            pay = await self._get(conn, "sim_payments", payment_id, lock=True)
            if key:
                found = await self._one(
                    conn,
                    "SELECT * FROM billing_sim.sim_refunds WHERE payment_id = :p "
                    "AND notes->>'refund_key' = :k",
                    p=payment_id, k=key,
                )  # fmt: skip
                if found is not None:
                    return _refund_dto(found)
            pending = (
                await self._one(
                    conn,
                    "SELECT coalesce(sum(amount), 0) AS n FROM billing_sim.sim_refunds "
                    "WHERE payment_id = :p AND status = 'pending'",
                    p=payment_id,
                )
                or {"n": 0}
            )["n"]
            if pay["status"] not in ("captured", "refunded") or (
                int(amount) > int(pay["amount"]) - int(pay["amount_refunded"]) - int(pending)
            ):
                raise _reject("The refund amount provided is greater than amount captured")
            now = self._now()
            refund_id = ev.new_id("rfnd")
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_refunds (id, payment_id, amount, status, notes, "
                "created_at, process_after) VALUES (:id, :p, :a, 'pending', CAST(:n AS jsonb), "
                ":c, :after)",
                id=refund_id, p=payment_id, a=int(amount), n=_j(notes), c=now,
                after=now + timedelta(seconds=self.config.refund_delay),
            )  # fmt: skip
            return _refund_dto(await self._get(conn, "sim_refunds", refund_id))

    async def fetch_refund(self, refund_id: str) -> GatewayRefund:
        async with self._engine.connect() as conn:
            return _refund_dto(await self._get(conn, "sim_refunds", refund_id))

    async def process_refunds(self) -> int:
        """Pending refunds past their delay become processed (job sim.refunds)."""
        done = 0
        async with self._engine.begin() as conn:
            due = await self._all(
                conn,
                "SELECT id FROM billing_sim.sim_refunds WHERE status = 'pending' "
                "AND process_after <= :n ORDER BY created_at FOR UPDATE SKIP LOCKED",
                n=self._now(),
            )
            for r in due:
                await self._finish_refund(conn, r["id"], ok=True)
                done += 1
        return done

    async def fail_refund(self, refund_id: str) -> GatewayRefund:
        async with self._engine.begin() as conn:
            refund = await self._get(conn, "sim_refunds", refund_id, lock=True)
            if refund["status"] != "pending":
                raise _reject(f"Refund is {refund['status']}, not pending")
            await self._finish_refund(conn, refund_id, ok=False)
            return _refund_dto(await self._get(conn, "sim_refunds", refund_id))

    async def _finish_refund(self, conn: AsyncConnection, refund_id: str, *, ok: bool) -> None:
        refund = await self._get(conn, "sim_refunds", refund_id)
        await self._run(
            conn,
            "UPDATE billing_sim.sim_refunds SET status = :s, processed_at = :n WHERE id = :i",
            s="processed" if ok else "failed", n=self._now(), i=refund_id,
        )  # fmt: skip
        if ok:
            await self._run(
                conn,
                "UPDATE billing_sim.sim_payments SET amount_refunded = amount_refunded + :a, "
                "status = CASE WHEN amount_refunded + :a >= amount THEN 'refunded' ELSE status END "
                "WHERE id = :p",
                a=int(refund["amount"]), p=refund["payment_id"],
            )  # fmt: skip
        refund = await self._get(conn, "sim_refunds", refund_id)
        pay = await self._get(conn, "sim_payments", refund["payment_id"])
        await self._emit(
            conn,
            "refund.processed" if ok else "refund.failed",
            refund=ev.refund_entity(refund),
            payment=ev.payment_entity(pay),
        )

    # --- orders and payment links --------------------------------------------------------

    async def create_order(
        self, amount: Paise, *, receipt: str, notes: Mapping[str, str]
    ) -> GatewayOrder:
        async with self._engine.begin() as conn:
            found = await self._one(
                conn, "SELECT * FROM billing_sim.sim_orders WHERE receipt = :r", r=receipt
            )
            if found is not None:
                return _order_dto(found)
            order_id = ev.new_id("order")
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_orders (id, amount, receipt, notes, status, "
                "created_at) VALUES (:id, :a, :r, CAST(:n AS jsonb), 'created', :c)",
                id=order_id, a=int(amount), r=receipt, n=_j(notes), c=self._now(),
            )  # fmt: skip
            return _order_dto(await self._get(conn, "sim_orders", order_id))

    async def fetch_order(self, order_id: str) -> GatewayOrder:
        async with self._engine.connect() as conn:
            return _order_dto(await self._get(conn, "sim_orders", order_id))

    async def create_payment_link(
        self,
        amount: Paise,
        *,
        description: str,
        customer_name: str,
        customer_email: str | None,
        reference_id: str,
        notes: Mapping[str, str],
    ) -> GatewayPaymentLink:
        async with self._engine.begin() as conn:
            found = await self._one(
                conn,
                "SELECT * FROM billing_sim.sim_payment_links WHERE reference_id = :r",
                r=reference_id,
            )
            if found is not None:
                return _link_dto(found)
            link_id = ev.new_id("plink")
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_payment_links (id, amount, reference_id, status, "
                "description, customer_name, customer_email, notes, short_url, created_at) VALUES "
                "(:id, :a, :r, 'created', :d, :cn, :ce, CAST(:n AS jsonb), :u, :c)",
                id=link_id, a=int(amount), r=reference_id, d=description, cn=customer_name,
                ce=customer_email, n=_j(notes), u=self.checkout_url(link_id), c=self._now(),
            )  # fmt: skip
            return _link_dto(await self._get(conn, "sim_payment_links", link_id))

    async def cancel_payment_link(self, link_id: str) -> None:
        async with self._engine.begin() as conn:
            link = await self._get(conn, "sim_payment_links", link_id, lock=True)
            if link["status"] != "paid":
                await self._run(
                    conn,
                    "UPDATE billing_sim.sim_payment_links SET status = 'cancelled' WHERE id = :i",
                    i=link_id,
                )

    # --- settlements ---------------------------------------------------------------------

    async def list_settlement_items(self, day: date) -> list[SettlementItem]:
        async with self._engine.connect() as conn:
            rows = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_settlement_items "
                "WHERE (settled_at AT TIME ZONE 'Asia/Kolkata')::date = :d ORDER BY id",
                d=day,
            )
        return [
            SettlementItem(
                entity_id=r["entity_id"],
                kind=r["kind"],
                amount=Paise(int(r["amount"])),
                credit=Paise(int(r["credit"])),
                debit=Paise(int(r["debit"])),
                fee=Paise(int(r["fee"])),
                tax=Paise(int(r["tax"])),
                settled=True,
                settled_at=r["settled_at"],
                settlement_id=r["settlement_id"],
                payment_id=r["payment_id"],
            )
            for r in rows
        ]

    async def settle(self, *, everything: bool = False) -> str | None:
        """Settle captured payments and processed refunds not yet settled: payments after T+2 days
        (scaled), or all of them with `everything`. Returns the settlement id, or None."""
        now = self._now()
        cutoff = now if everything else now - SETTLE_AFTER * self.config.scale
        async with self._engine.begin() as conn:
            pays = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_payments p WHERE status IN ('captured', 'refunded') "
                "AND created_at <= :c AND NOT EXISTS (SELECT 1 FROM "
                "billing_sim.sim_settlement_items i WHERE i.entity_id = p.id) "
                "ORDER BY created_at FOR UPDATE",
                c=cutoff,
            )
            refunds = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_refunds r WHERE status = 'processed' "
                "AND processed_at <= :c AND NOT EXISTS (SELECT 1 FROM "
                "billing_sim.sim_settlement_items i WHERE i.entity_id = r.id) "
                "ORDER BY created_at FOR UPDATE",
                c=cutoff,
            )
            if not pays and not refunds:
                return None
            setl_id = ev.new_id("setl")
            gross = fee_total = tax_total = refunded = 0
            items: list[tuple[Any, ...]] = []
            for p in pays:
                fee = int(p["fee"] or 0)
                tax = int(p["tax"] or 0)
                gross += int(p["amount"])
                fee_total += fee
                tax_total += tax
                items.append(
                    (
                        p["id"],
                        "payment",
                        int(p["amount"]),
                        int(p["amount"]) - fee,
                        0,
                        fee,
                        tax,
                        None,
                    )
                )
            for r in refunds:
                refunded += int(r["amount"])
                items.append(
                    (
                        r["id"],
                        "refund",
                        int(r["amount"]),
                        0,
                        int(r["amount"]),
                        0,
                        0,
                        r["payment_id"],
                    )
                )  # noqa: E501
            await self._run(
                conn,
                "INSERT INTO billing_sim.sim_settlements (id, day, gross, fee, tax, net, "
                "created_at) VALUES (:id, :d, :g, :f, :t, :n, :c)",
                id=setl_id, d=now.astimezone(IST).date(), g=gross, f=fee_total, t=tax_total,
                n=gross - fee_total - refunded, c=now,
            )  # fmt: skip
            for entity_id, kind, amount, credit, debit, fee, tax, pay_id in items:
                await self._run(
                    conn,
                    "INSERT INTO billing_sim.sim_settlement_items (settlement_id, entity_id, kind, "
                    "amount, credit, debit, fee, tax, payment_id, settled_at) VALUES "
                    "(:s, :e, :k, :a, :cr, :db, :f, :t, :p, :n)",
                    s=setl_id, e=entity_id, k=kind, a=amount, cr=credit, db=debit, f=fee, t=tax,
                    p=pay_id, n=now,
                )  # fmt: skip
            return setl_id

    # --- signatures (the same helpers the Razorpay adapter uses) -------------------------

    def verify_order_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        from billing.adapters.razorpay import signatures

        return signatures.verify_order(self.key_secret, order_id, payment_id, signature)

    def verify_checkout_signature(
        self, payment_id: str, subscription_id: str, signature: str
    ) -> bool:
        from billing.adapters.razorpay import signatures

        return signatures.verify_checkout(self.key_secret, payment_id, subscription_id, signature)

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        from billing.adapters.razorpay import signatures

        return signatures.verify_webhook(self.webhook_secrets, raw_body, signature)

    def verify_payment_link_signature(
        self, link_id: str, reference_id: str, status: str, payment_id: str, signature: str
    ) -> bool:
        expected = ev.payment_link_signature(
            self.key_secret, link_id, reference_id, status, payment_id
        )
        return hmac.compare_digest(expected.encode(), signature.encode())

    # --- checkout (what the simulated page does) -----------------------------------------

    def make_token(self, gateway_id: str, user_id: str, *, ttl_seconds: int = 1800) -> str:
        expiry = int(self._now().timestamp()) + ttl_seconds
        mac = ev.sign(self.key_secret, f"{gateway_id}|{user_id}|{expiry}".encode())
        return f"{user_id}.{expiry}.{mac}"

    def check_token(self, gateway_id: str, token: str) -> bool:
        user_id, _, rest = token.partition(".")
        expiry, _, mac = rest.partition(".")
        if not (user_id and expiry.isdigit() and mac):
            return False
        expected = ev.sign(self.key_secret, f"{gateway_id}|{user_id}|{expiry}".encode())
        return hmac.compare_digest(expected.encode(), mac.encode()) and int(expiry) >= int(
            self._now().timestamp()
        )

    def signed_url(self, gateway_id: str, user_id: str) -> str:
        return f"{self.checkout_url(gateway_id)}?t={self.make_token(gateway_id, user_id)}"

    async def checkout_info(self, gateway_id: str) -> Row:
        """What the page shows: kind, amount, a description, and the methods on offer."""
        async with self._engine.connect() as conn:
            if gateway_id.startswith("sub_"):
                sub = await self._get(conn, "sim_subscriptions", gateway_id)
                plan = await self._get(conn, "sim_plans", sub["plan_id"])
                amount, kind, label, status = (
                    int(plan["amount"]),
                    "subscription",
                    plan["name"],
                    sub["status"],
                )  # noqa: E501
                if sub["status"] == "created" and sub["start_at"] and sub["start_at"] > self._now():
                    amount = AUTH_AMOUNT  # a later start: only the mandate is authorised today
            elif gateway_id.startswith("order_"):
                order = await self._get(conn, "sim_orders", gateway_id)
                amount, kind, status = int(order["amount"]), "order", order["status"]
                label = "Wallet top-up"
            elif gateway_id.startswith("plink_"):
                link = await self._get(conn, "sim_payment_links", gateway_id)
                amount, kind, status = int(link["amount"]), "payment_link", link["status"]
                label = link["description"] or "Invoice payment"
            else:
                raise _reject()
        methods = ["card", "emandate"] + ([] if amount >= UPI_LIMIT else ["upi"])
        return {"id": gateway_id, "kind": kind, "amount": amount, "label": label,
                "status": status, "methods": methods}  # fmt: skip

    @staticmethod
    def _instrument_error(
        method: str, amount: int, card_number: str, vpa: str, otp: str
    ) -> str | None:
        if method == "upi" and amount >= UPI_LIMIT:
            return "UPI is not available for payments of Rs 15,000 or more"
        if method == "card":
            if card_number.replace(" ", "") != CARD_OK:
                return "Card declined (use 4111 1111 1111 1111 to succeed)"
        elif method == "upi":
            if vpa != "success@razorpay":
                return "UPI payment failed (use success@razorpay to succeed)"
        elif method != "emandate":
            return f"unsupported payment method {method!r}"
        if not (len(otp) == 6 and otp.isdigit()) or otp == "000000":
            return "Authentication failed: wrong OTP"
        return None

    async def pay(
        self,
        gateway_id: str,
        *,
        method: str,
        card_number: str = "",
        vpa: str = "",
        otp: str = "",
    ) -> PayResult:
        """The customer pays the subscription, order or payment link on the simulated page."""
        info = await self.checkout_info(gateway_id)
        error = self._instrument_error(method, info["amount"], card_number, vpa, otp)
        async with self._engine.begin() as conn:
            if gateway_id.startswith("sub_"):
                return await self._pay_subscription(conn, gateway_id, method, error)
            if gateway_id.startswith("order_"):
                return await self._pay_order(conn, gateway_id, method, error)
            return await self._pay_link(conn, gateway_id, method, error)

    async def _new_payment(
        self,
        conn: AsyncConnection,
        *,
        amount: int,
        status: str,
        method: str,
        description: str | None = None,
        order_id: str | None = None,
        subscription_id: str | None = None,
        link_id: str | None = None,
        notes: Mapping[str, str] | None = None,
        error: str | None = None,
    ) -> Row:
        captured = status == "captured"
        fee, tax = ev.fee_of(amount) if captured else (None, None)
        pay_id = ev.new_id("pay")
        await self._run(
            conn,
            "INSERT INTO billing_sim.sim_payments (id, amount, status, method, fee, tax, order_id, "
            "invoice_id, subscription_id, link_id, description, notes, error_code, "
            "error_description, created_at) VALUES (:id, :a, :s, :m, :f, :t, :o, :inv, :sub, :l, "
            ":d, CAST(:n AS jsonb), :ec, :ed, :c)",
            id=pay_id, a=amount, s=status, m=method, f=fee, t=tax, o=order_id,
            inv=ev.new_id("inv") if subscription_id and captured else None, sub=subscription_id,
            l=link_id, d=description, n=_j(notes),
            ec="BAD_REQUEST_ERROR" if error else None, ed=error, c=self._now(),
        )  # fmt: skip
        return await self._get(conn, "sim_payments", pay_id)

    async def _fail(self, conn: AsyncConnection, error: str, **payment: Any) -> PayResult:
        pay = await self._new_payment(conn, status="failed", error=error, **payment)
        await self._emit(conn, "payment.failed", payment=ev.payment_entity(pay))
        return PayResult(False, pay["id"], error)

    async def _pay_subscription(
        self, conn: AsyncConnection, sub_id: str, method: str, error: str | None
    ) -> PayResult:
        sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
        plan = await self._get(conn, "sim_plans", sub["plan_id"])
        now = self._now()
        if sub["status"] not in ("created", "pending", "halted"):
            raise _reject(f"Subscription is {sub['status']}; there is nothing to pay")
        later = sub["status"] == "created" and sub["start_at"] and sub["start_at"] > now
        amount = AUTH_AMOUNT if later else int(plan["amount"])
        if error:
            return await self._fail(
                conn, error, amount=amount, method=method, subscription_id=sub_id,
                description=plan["name"],
            )  # fmt: skip
        if later:  # authorise the mandate; the first charge happens at start_at
            pay = await self._new_payment(
                conn, amount=amount, status="authorized", method=method,
                subscription_id=sub_id, description=plan["name"],
            )  # fmt: skip
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET status = 'authenticated', method = :m, "
                "charge_at = start_at WHERE id = :i",
                m=method,
                i=sub_id,
            )
            await self._emit_sub(conn, "subscription.authenticated", sub_id)
        else:
            first = sub["status"] == "created"
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET method = :m WHERE id = :i",
                m=method, i=sub_id,
            )  # fmt: skip
            if first:
                await self._run(
                    conn,
                    "UPDATE billing_sim.sim_subscriptions SET status = 'authenticated' "
                    "WHERE id = :i",
                    i=sub_id,
                )
                await self._emit_sub(conn, "subscription.authenticated", sub_id)
            pay = await self._charge(conn, sub_id, method, activate=first)
        return PayResult(
            True,
            pay["id"],
            handler={
                "razorpay_payment_id": pay["id"],
                "razorpay_subscription_id": sub_id,
                "razorpay_signature": ev.checkout_signature(self.key_secret, pay["id"], sub_id),
            },
        )

    async def _pay_order(
        self, conn: AsyncConnection, order_id: str, method: str, error: str | None
    ) -> PayResult:
        order = await self._get(conn, "sim_orders", order_id, lock=True)
        if order["status"] == "paid":
            raise _reject("This order is already paid")
        kw: dict[str, Any] = {
            "amount": int(order["amount"]),
            "method": method,
            "order_id": order_id,
        }
        if error:
            return await self._fail(conn, error, **kw)
        pay = await self._new_payment(conn, status="captured", **kw)
        await self._run(
            conn, "UPDATE billing_sim.sim_orders SET status = 'paid' WHERE id = :i", i=order_id
        )
        order = await self._get(conn, "sim_orders", order_id)
        await self._emit(conn, "payment.captured", payment=ev.payment_entity(pay))
        await self._emit(
            conn, "order.paid", order=ev.order_entity(order), payment=ev.payment_entity(pay)
        )
        return PayResult(
            True,
            pay["id"],
            handler={
                "razorpay_payment_id": pay["id"],
                "razorpay_order_id": order_id,
                "razorpay_signature": ev.order_signature(self.key_secret, order_id, pay["id"]),
            },
        )

    async def _pay_link(
        self, conn: AsyncConnection, link_id: str, method: str, error: str | None
    ) -> PayResult:
        link = await self._get(conn, "sim_payment_links", link_id, lock=True)
        if link["status"] != "created":
            raise _reject(f"Payment link is {link['status']}")
        kw: dict[str, Any] = {
            "amount": int(link["amount"]), "method": method, "link_id": link_id,
            "description": link["description"], "order_id": ev.new_id("order"),
        }  # fmt: skip
        if error:
            return await self._fail(conn, error, **kw)
        pay = await self._new_payment(conn, status="captured", **kw)
        await self._run(
            conn,
            "UPDATE billing_sim.sim_payment_links SET status = 'paid' WHERE id = :i",
            i=link_id,
        )
        link = await self._get(conn, "sim_payment_links", link_id)
        await self._emit(
            conn,
            "payment_link.paid",
            payment_link=ev.payment_link_entity(link),
            payment=ev.payment_entity(pay, short=True),
        )
        ref = link["reference_id"] or ""
        return PayResult(
            True,
            pay["id"],
            handler={
                "razorpay_payment_id": pay["id"],
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_reference_id": ref,
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": ev.payment_link_signature(
                    self.key_secret, link_id, ref, "paid", pay["id"]
                ),
            },
        )

    # --- charging and renewals -----------------------------------------------------------

    async def _charge(
        self,
        conn: AsyncConnection,
        sub_id: str,
        method: str | None = None,
        *,
        activate: bool = False,
    ) -> Row:
        """A successful charge of the next period: payment, new period, `subscription.charged`."""
        sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
        plan = await self._get(conn, "sim_plans", sub["plan_id"])
        now = self._now()
        if sub["paid_count"] > 0 and sub["current_end"] is not None:
            start = sub["current_end"]
        else:
            start = sub["start_at"] if sub["start_at"] and sub["start_at"] <= now else now
        end = self.period_end(plan, start)
        order_id = ev.new_id("order")
        await self._run(
            conn,
            "INSERT INTO billing_sim.sim_orders (id, amount, status, created_at) "
            "VALUES (:i, :a, 'paid', :c)",
            i=order_id, a=int(plan["amount"]), c=now,
        )  # fmt: skip
        pay = await self._new_payment(
            conn, amount=int(plan["amount"]), status="captured",
            method=method or sub["method"] or "card", order_id=order_id,
            subscription_id=sub_id, description=plan["name"],
        )  # fmt: skip
        paid = sub["paid_count"] + 1
        done = paid >= sub["total_count"]
        await self._run(
            conn,
            "UPDATE billing_sim.sim_subscriptions SET status = 'active', paid_count = :p, "
            "current_start = :s, current_end = :e, charge_at = :e, retry_count = 0, "
            "next_retry_at = NULL, next_outcome = 'success' WHERE id = :i",
            p=paid, s=start, e=end, i=sub_id,
        )  # fmt: skip
        if activate:
            await self._emit_sub(conn, "subscription.activated", sub_id)
        await self._emit_sub(conn, "subscription.charged", sub_id, payment=ev.payment_entity(pay))
        if done:
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET status = 'completed', charge_at = NULL, "
                "ended_at = :n WHERE id = :i",
                n=now, i=sub_id,
            )  # fmt: skip
            await self._emit_sub(
                conn, "subscription.completed", sub_id, payment=ev.payment_entity(pay)
            )
        return pay

    async def renew(self, sub_id: str, *, force: bool = False) -> str:
        """One renewal step for a subscription (see plan 5.4). Returns its status afterwards."""
        async with self._engine.begin() as conn:
            sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
            now = self._now()
            status = str(sub["status"])
            if status not in ("authenticated", "active", "pending"):
                raise _reject(f"Subscription is {status}; it cannot be charged")
            if status == "pending":
                if not force and (sub["next_retry_at"] is None or sub["next_retry_at"] > now):
                    return status
            elif not force and (sub["charge_at"] is None or sub["charge_at"] > now):
                return status
            if status == "active" and sub["cancel_at_cycle_end"]:
                await self._end_cancelled(conn, sub)
                return "cancelled"
            outcome = sub["next_outcome"]
            if outcome == "success":
                await self._charge(conn, sub_id, activate=status == "authenticated")
            elif outcome == "halt":
                await self._halt(conn, sub_id, via_pending=status != "pending")
            elif status == "pending":  # a retry that fails again
                retries = sub["retry_count"] + 1
                if retries >= MAX_RETRIES:
                    await self._halt(conn, sub_id, via_pending=False)
                else:
                    await self._run(
                        conn,
                        "UPDATE billing_sim.sim_subscriptions SET retry_count = :r, "
                        "next_retry_at = :n WHERE id = :i",
                        r=retries, n=now + self._retry_delta(), i=sub_id,
                    )  # fmt: skip
            else:  # the first failure of this cycle
                await self._run(
                    conn,
                    "UPDATE billing_sim.sim_subscriptions SET status = 'pending', "
                    "retry_count = 0, next_retry_at = :n WHERE id = :i",
                    n=now + self._retry_delta(), i=sub_id,
                )  # fmt: skip
                await self._emit_sub(conn, "subscription.pending", sub_id)
            return str((await self._get(conn, "sim_subscriptions", sub_id))["status"])

    async def _halt(self, conn: AsyncConnection, sub_id: str, *, via_pending: bool) -> None:
        if via_pending:
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET status = 'pending' WHERE id = :i",
                i=sub_id,
            )
            await self._emit_sub(conn, "subscription.pending", sub_id)
        await self._run(
            conn,
            "UPDATE billing_sim.sim_subscriptions SET status = 'halted', charge_at = NULL, "
            "next_retry_at = NULL WHERE id = :i",
            i=sub_id,
        )
        await self._emit_sub(conn, "subscription.halted", sub_id)

    async def renew_due(self, limit: int = 100) -> int:
        """Job sim.renew: charge every subscription whose time has come."""
        now = self._now()
        async with self._engine.connect() as conn:
            due = await self._all(
                conn,
                "SELECT id FROM billing_sim.sim_subscriptions WHERE "
                "(status IN ('active', 'authenticated') AND charge_at <= :n) "
                "OR (status = 'pending' AND next_retry_at <= :n) ORDER BY charge_at LIMIT :l",
                n=now, l=limit,
            )  # fmt: skip
            soon = await self._all(
                conn,
                "SELECT id, charge_at FROM billing_sim.sim_subscriptions WHERE status = 'active' "
                "AND charge_at > :n AND charge_at <= :n + make_interval(secs => :w) "
                "AND notice_sent_for IS DISTINCT FROM charge_at",
                n=now, w=PRE_DEBIT_NOTICE.total_seconds() * self.config.scale,
            )  # fmt: skip
        done = 0
        for r in due:
            try:
                await self.renew(r["id"])
                done += 1
            except GatewayRejected:
                continue  # it changed under us
        for r in soon:
            log.info("sim_pre_debit_notice", subscription_id=r["id"], charge_at=r["charge_at"])
            async with self._engine.begin() as conn:
                await self._run(
                    conn,
                    "UPDATE billing_sim.sim_subscriptions SET notice_sent_for = :c WHERE id = :i",
                    c=r["charge_at"],
                    i=r["id"],
                )
        return done

    # --- control (simulator API and customer actions) ------------------------------------

    async def set_next_outcome(self, sub_id: str, outcome: str) -> GatewaySubscription:
        if outcome not in ("success", "fail", "halt"):
            raise _reject("outcome must be success, fail or halt")
        async with self._engine.begin() as conn:
            await self._get(conn, "sim_subscriptions", sub_id)
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET next_outcome = :o WHERE id = :i",
                o=outcome, i=sub_id,
            )  # fmt: skip
            return _sub_dto(await self._get(conn, "sim_subscriptions", sub_id))

    async def customer_cancel(self, sub_id: str) -> GatewaySubscription:
        """The customer cancels the mandate from their UPI app."""
        async with self._engine.begin() as conn:
            sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
            if sub["status"] in NOT_CANCELLABLE:
                raise _reject(f"Subscription is not cancellable in {sub['status']} status")
            await self._end_cancelled(conn, sub)
            return _sub_dto(await self._get(conn, "sim_subscriptions", sub_id))

    async def customer_pause(self, sub_id: str) -> GatewaySubscription:
        async with self._engine.begin() as conn:
            sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
            if sub["status"] != "active":
                raise _reject(f"Subscription is {sub['status']}; only an active one can pause")
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET status = 'paused' WHERE id = :i",
                i=sub_id,
            )
            await self._emit_sub(conn, "subscription.paused", sub_id)
            return _sub_dto(await self._get(conn, "sim_subscriptions", sub_id))

    async def customer_resume(self, sub_id: str) -> GatewaySubscription:
        async with self._engine.begin() as conn:
            sub = await self._get(conn, "sim_subscriptions", sub_id, lock=True)
            if sub["status"] != "paused":
                raise _reject(f"Subscription is {sub['status']}; only a paused one can resume")
            await self._run(
                conn,
                "UPDATE billing_sim.sim_subscriptions SET status = 'active' WHERE id = :i",
                i=sub_id,
            )
            await self._emit_sub(conn, "subscription.resumed", sub_id)
            return _sub_dto(await self._get(conn, "sim_subscriptions", sub_id))

    async def state(self, user_id: str) -> dict[str, Any]:
        """One dump of a user's simulated subscriptions, payments, refunds and pending webhooks."""
        async with self._engine.connect() as conn:
            subs = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_subscriptions WHERE notes->>'user_id' = :u "
                "ORDER BY created_at",
                u=user_id,
            )
            ids = [s["id"] for s in subs]
            pays = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_payments WHERE subscription_id = ANY(:ids) "
                "ORDER BY created_at",
                ids=ids,
            )
            refunds = await self._all(
                conn,
                "SELECT * FROM billing_sim.sim_refunds WHERE payment_id = ANY(:p) "
                "ORDER BY created_at",
                p=[p["id"] for p in pays],
            )
            hooks = await self._all(
                conn,
                "SELECT id, event_id, event, status, attempts, deliver_after, delivered_at "
                "FROM billing_sim.sim_webhook_outbox WHERE status IN ('pending', 'dropped') "
                "ORDER BY id",
            )
        return {"subscriptions": subs, "payments": pays, "refunds": refunds,
                "pending_webhooks": hooks}  # fmt: skip
