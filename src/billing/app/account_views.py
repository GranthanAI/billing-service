"""What a signed-in user sees of their own billing: /me, usage, invoices, invoice PDFs."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from billing.app.entitlements import EntitlementService
from billing.app.errors import GatewayRejected, GatewayUnavailable, NotFound, ValidationFailed
from billing.app.ports import Clock, Counters, ObjectStorage, PaymentGateway, UnitOfWork
from billing.app.wallet import wallet_block
from billing.domain.calendar import IST, ist_day, next_ist_midnight
from billing.domain.catalog import OwnerType
from billing.domain.subscription import SubscriptionState, SubStatus

PDF_LINK_SECONDS = 300
MAX_USAGE_DAYS = 366
INVOICE_LIMIT = 100


def _iso(dt: object) -> str | None:
    return dt.astimezone(IST).isoformat() if dt is not None else None  # type: ignore[attr-defined]


class AccountViews:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        entitlements: EntitlementService,
        counters: Counters,
        storage: ObjectStorage,
        clock: Clock,
        gst_registered: bool = True,
        gateway: PaymentGateway | None = None,
        payment_mode: str = "live",
    ) -> None:
        self._gateway = gateway
        self._payment_mode = payment_mode
        self._registered = gst_registered
        self._uow = uow_factory
        self._ent = entitlements
        self._counters = counters
        self._storage = storage
        self._clock = clock

    async def me(self, user_id: UUID) -> dict[str, Any]:
        now = self._clock.now()
        ent = await self._ent.resolve(user_id)
        plan = ent.grant.plan_version
        sub_state: SubscriptionState | None = None
        wallet_view: dict[str, Any] | None = None
        pending: dict[str, Any] | None = None
        async with self._uow() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            sub = await uow.subscriptions.live_for_account(account.id) if account else None
            wallet = await uow.wallets.get(account.id) if account else None
            if account is not None and wallet is not None:
                wallet_view = wallet_block(
                    self._registered,
                    await uow.wallets.balance(account.id),
                    wallet.monthly_cap_paise,
                )
            if sub is not None:
                sub_state = sub.state
                change = await uow.changes.open_for_subscription(sub.id)
                target = sub_state.pending_plan_version_id or (
                    change.new_plan_version_id if change else None
                )
                if target is not None:
                    new = await uow.plans.by_id(target)
                    pending = {
                        "kind": change.kind.value if change else "downgrade",
                        "plan_code": new.plan.plan_code if new else None,
                        "interval": (
                            sub_state.pending_interval or (change.new_interval if change else None)
                        ),
                        "status": change.status.value if change else "authorised",
                    }
                    if pending["interval"] is not None:
                        pending["interval"] = pending["interval"].value
        counts = await self._counters.day_counts(user_id, ist_day(now))
        month = await self._counters.month_counts(user_id, now)
        return {
            "plan": {
                "code": plan.plan_code,
                "version": plan.version,
                "interval": sub_state.interval.value if sub_state else None,
                "source": ent.grant.source.value,
            },
            "status": sub_state.status.value if sub_state else "free",
            "current_period_end": _iso(sub_state.current_period_end) if sub_state else None,
            "grace_until": _iso(sub_state.grace_until) if sub_state else None,
            "cancel_at_period_end": bool(sub_state and sub_state.status is SubStatus.CANCELLING),
            "pending_change": pending,
            "limits": asdict(ent.limits),  # every plan limit; -1 means unlimited
            "payment_mode": self._payment_mode,
            "payment_action_url": await self._action_url(user_id, sub_state),
            "usage_today": {
                "total": counts.get("total", 0),
                "fast": counts.get("fast", 0),
                "smart": counts.get("smart", 0),
                "best": counts.get("best", 0),
                "ultra": counts.get("ultra", 0),
                "resets_at": _iso(next_ist_midnight(now)),
            },
            "usage_month": {"youtube_videos": month.get("youtube", 0)},
            "wallet": wallet_view,
        }

    async def _action_url(self, user_id: UUID, state: SubscriptionState | None) -> str | None:
        """Where a past_due or suspended user pays: the gateway subscription's short_url (in
        simulation, a signed link to the simulated pay page)."""
        gid = state.gateway_subscription_id if state else None
        if (
            self._gateway is None
            or state is None
            or gid is None
            or state.status not in (SubStatus.PAST_DUE, SubStatus.SUSPENDED)
        ):
            return None
        signed = getattr(self._gateway, "signed_url", None)
        if signed is not None:
            return str(signed(gid, str(user_id)))
        try:
            return (await self._gateway.fetch_subscription(gid)).short_url
        except (GatewayRejected, GatewayUnavailable):
            return None

    async def usage(self, user_id: UUID, start: date | None, end: date | None) -> dict[str, Any]:
        today = ist_day(self._clock.now())
        end = end or today
        start = start or end - timedelta(days=29)
        if start > end:
            raise ValidationFailed("from must not be after to")
        if (end - start).days >= MAX_USAGE_DAYS:
            raise ValidationFailed(f"the range is at most {MAX_USAGE_DAYS} days")
        async with self._uow() as uow:
            rows = await uow.admin.daily_usage(user_id, start, end)
        days: dict[str, dict[str, Any]] = {}
        for r in rows:
            day = days.setdefault(r["day"].isoformat(), {"day": r["day"].isoformat(), "groups": {}})
            day["groups"][r["model_group"]] = {
                "messages": r["messages"],
                "billable_units": r["billable_units"],
            }
        return {"from": start.isoformat(), "to": end.isoformat(), "days": list(days.values())}

    async def invoices(self, user_id: UUID) -> dict[str, Any]:
        async with self._uow() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            if account is None:
                return {"invoices": []}
            found = await uow.invoices.for_account(account.id, INVOICE_LIMIT)
            originals = {
                i.original_invoice_id: await uow.invoices.get(i.original_invoice_id)
                for i in found
                if i.original_invoice_id
            }
        return {
            "invoices": [
                {
                    "id": str(i.id),
                    "number": i.number,
                    "kind": i.kind,
                    "issued_at": _iso(i.issued_at),
                    "taxable_paise": int(i.taxable_paise),
                    "cgst_paise": int(i.cgst_paise),
                    "sgst_paise": int(i.sgst_paise),
                    "igst_paise": int(i.igst_paise),
                    "total_paise": int(i.total_paise),
                    "status": i.status,
                    "original_number": (
                        originals[i.original_invoice_id].number  # type: ignore[union-attr]
                        if i.original_invoice_id and originals.get(i.original_invoice_id)
                        else None
                    ),
                    "pdf_ready": i.pdf_object_key is not None,
                }
                for i in found
            ]
        }

    async def pdf_url(self, user_id: UUID, invoice_id: UUID) -> str:
        """A 5-minute link to the PDF. Someone else's invoice is a 404, not a 403: it should not
        be possible to learn that an invoice id exists."""
        async with self._uow() as uow:
            account = await uow.accounts.get_by_owner(OwnerType.USER, user_id)
            inv = await uow.invoices.get(invoice_id)
        if account is None or inv is None or inv.billing_account_id != account.id:
            raise NotFound("invoice not found")
        if inv.pdf_object_key is None:
            raise NotFound("the PDF is still being prepared; try again in a minute")
        return await self._storage.presigned_get(inv.pdf_object_key, PDF_LINK_SECONDS)
