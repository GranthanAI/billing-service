"""Staff actions. Every write needs a reason and leaves an `admin_audit` row with before/after."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from billing.app.effects import REFUND_ISSUE as REFUND_JOB
from billing.app.errors import NotFound, ValidationFailed
from billing.app.ports import Clock, IdGenerator, UnitOfWork
from billing.app.records import OverrideRecord
from billing.app.settlements import SettlementService
from billing.domain.calendar import IST
from billing.domain.catalog import Interval, ModelGroup, OwnerType
from billing.domain.money import Paise
from billing.domain.proration import period_interval

LIMITS_ONLY_ACTION = "catalog.limits_only_change"
MIN_REASON = 3
MAX_COST_DAYS = 366


@dataclass(frozen=True, slots=True)
class Staff:
    id: UUID
    role: str  # billing_admin | support

    @property
    def can_write(self) -> bool:
        return self.role == "billing_admin"


def need_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < MIN_REASON:
        raise ValidationFailed("a reason is required for every admin change")
    return cleaned


class AdminService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        ids: IdGenerator,
        settlements: SettlementService,
        invalidate: Callable[[UUID], Awaitable[None]],
    ) -> None:
        self._uow = uow_factory
        self._clock = clock
        self._ids = ids
        self._settlements = settlements
        self._invalidate = invalidate

    def _forget(self, uow: UnitOfWork, owner_type: str, owner_id: UUID) -> None:
        if owner_type == OwnerType.USER.value:
            uow.after_commit(lambda: self._invalidate(owner_id))

    # --- reads -------------------------------------------------------------------------------

    async def search(self, q: str) -> list[dict[str, Any]]:
        if not q.strip():
            raise ValidationFailed("q is required")
        async with self._uow() as uow:
            return await uow.admin.search_accounts(q.strip())

    async def account(self, account_id: UUID) -> dict[str, Any]:
        now = self._clock.now().astimezone(IST)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        async with self._uow() as uow:
            detail = await uow.admin.account_detail(account_id, start, end)
        if detail is None:
            raise NotFound("account not found")
        return detail

    async def costs(self, start: date, end: date, by: str) -> list[dict[str, Any]]:
        if by not in ("plan", "provider", "user"):
            raise ValidationFailed("by must be plan, provider or user")
        if start > end or (end - start).days >= MAX_COST_DAYS:
            raise ValidationFailed(
                f"from/to must be an ordered range of at most {MAX_COST_DAYS} days"
            )
        lo = datetime.combine(start, datetime.min.time(), tzinfo=IST)
        hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=IST)
        async with self._uow() as uow:
            return await uow.admin.costs(lo, hi, by)

    async def reconciliation(self, run_date: date) -> list[dict[str, Any]]:
        async with self._uow() as uow:
            return await uow.admin.latest_reconciliation(run_date)

    # --- overrides ---------------------------------------------------------------------------

    async def create_override(
        self,
        staff: Staff,
        owner_type: OwnerType,
        owner_id: UUID,
        plan_code: str,
        reason: str,
        expires_at: datetime,
        starts_at: datetime | None = None,
    ) -> OverrideRecord:
        reason = need_reason(reason)
        now = self._clock.now()
        starts = starts_at or now
        if expires_at <= starts:
            raise ValidationFailed("expires_at must be after the start")
        async with self._uow() as uow:
            plan = await uow.plans.current(plan_code, None)
            if plan is None:
                raise ValidationFailed(f"unknown plan {plan_code!r}")
            record = OverrideRecord(
                self._ids.new(), owner_type, owner_id, plan.plan.id, reason, starts, expires_at,
                staff.id,
            )  # fmt: skip
            await uow.overrides.insert(record)
            await uow.audit.record(
                staff.id, "override.create", "override", str(record.id), reason, None,
                {"owner_type": owner_type.value, "owner_id": str(owner_id), "plan": plan_code,
                 "expires_at": expires_at.isoformat()},
            )  # fmt: skip
            self._forget(uow, owner_type.value, owner_id)
        return record

    async def end_override(self, staff: Staff, override_id: UUID, reason: str) -> None:
        reason = need_reason(reason)
        async with self._uow() as uow:
            found = await uow.overrides.get(override_id)
            if found is None:
                raise NotFound("override not found")
            if found.ended_at is None:
                await uow.overrides.end(override_id, self._clock.now())
                await uow.audit.record(
                    staff.id, "override.end", "override", str(override_id), reason,
                    {"ended_at": None}, {"ended_at": self._clock.now().isoformat()},
                )  # fmt: skip
                self._forget(uow, found.owner_type.value, found.owner_id)

    # --- refunds -------------------------------------------------------------------------------

    async def refund(self, staff: Staff, payment_id: UUID, amount: Paise, reason: str) -> str:
        """Queue a refund (and its credit note) of at most what is still refundable."""
        reason = need_reason(reason)
        if amount <= 0:
            raise ValidationFailed("amount must be positive")
        async with self._uow() as uow:
            payment = await uow.payments.get(payment_id)
            if payment is None:
                raise NotFound("payment not found")
            done = await uow.refunds.total_for_payment(payment_id)
            remaining = int(payment.amount_paise) - int(done)
            if amount > remaining:
                raise ValidationFailed(f"at most {remaining} paise can still be refunded")
            # what the payment paid for, read off its invoice: the subscription may have moved
            # to another interval since (an upgrade, a downgrade), and a yearly charge sits in
            # deferred revenue while a monthly one does not
            invoice = await uow.invoices.by_payment(payment_id)
            interval = Interval.MONTH
            if invoice is not None and invoice.period_start and invoice.period_end:
                interval = period_interval(invoice.period_start, invoice.period_end)
            key = f"admin:{uuid4()}"
            await uow.jobs.enqueue(
                REFUND_JOB,
                {
                    "subscription_id": str(payment.subscription_id or ""),
                    "payment_id": str(payment_id),
                    "amount": int(amount),
                    "reason": reason,
                    "refund_key": key,
                    "interval": interval.value,
                },
                self._clock.now(),
                f"{REFUND_JOB}:{key}",
            )
            await uow.audit.record(
                staff.id, "refund.request", "payment", str(payment_id), reason,
                {"refunded_paise": int(done)}, {"requested_paise": int(amount), "refund_key": key},
            )  # fmt: skip
        return key

    # --- accounts ------------------------------------------------------------------------------

    async def block(self, staff: Staff, account_id: UUID, reason: str, *, blocked: bool) -> None:
        reason = need_reason(reason)
        async with self._uow() as uow:
            account = await uow.accounts.get(account_id)
            if account is None:
                raise NotFound("account not found")
            await uow.accounts.set_blocked(
                account_id, reason if blocked else None, self._clock.now()
            )
            await uow.audit.record(
                staff.id, "account.block" if blocked else "account.unblock", "account",
                str(account_id), reason,
                {"blocked": account.is_blocked}, {"blocked": blocked},
            )  # fmt: skip
            self._forget(uow, account.owner_type.value, account.owner_id)

    # --- prices --------------------------------------------------------------------------------

    async def put_model_price(
        self,
        staff: Staff,
        *,
        model: str,
        provider: str,
        group: ModelGroup,
        input_usd_per_m: Decimal,
        cached_input_usd_per_m: Decimal | None,
        output_usd_per_m: Decimal,
        effective_from: datetime,
        reason: str,
    ) -> None:
        reason = need_reason(reason)
        if min(input_usd_per_m, output_usd_per_m) < 0:
            raise ValidationFailed("prices must not be negative")
        async with self._uow() as uow:
            await uow.pricing.add_model_price(
                model=model, provider=provider, model_group=group,
                input_usd_per_m=input_usd_per_m, cached_input_usd_per_m=cached_input_usd_per_m,
                output_usd_per_m=output_usd_per_m, effective_from=effective_from,
            )  # fmt: skip
            await uow.audit.record(
                staff.id, "model_price.put", "model", model, reason, None,
                {"provider": provider, "group": group.value, "input": str(input_usd_per_m),
                 "output": str(output_usd_per_m), "effective_from": effective_from.isoformat()},
            )  # fmt: skip

    async def put_fx_rate(
        self, staff: Staff, effective_from: date, usd_inr: Decimal, source: str, reason: str
    ) -> None:
        reason = need_reason(reason)
        if usd_inr <= 0:
            raise ValidationFailed("usd_inr must be positive")
        async with self._uow() as uow:
            await uow.pricing.add_fx_rate(effective_from, usd_inr, source)
            await uow.audit.record(
                staff.id, "fx_rate.put", "fx_rate", effective_from.isoformat(), reason, None,
                {"usd_inr": str(usd_inr), "source": source},
            )  # fmt: skip

    # --- catalog and settlements ---------------------------------------------------------------

    async def apply_limits(self, staff: Staff, audit_id: int, reason: str) -> int:
        """Move live subscribers from the old version of a plan to the new one after a limits-only
        change. Returns how many owners were moved; a second call moves none."""
        reason = need_reason(reason)
        async with self._uow() as uow:
            row = await uow.admin.audit_row(audit_id)
            if row is None or row["action"] != LIMITS_ONLY_ACTION:
                raise NotFound("no limits-only catalog change with that audit id")
            old = UUID(row["before"]["plan_version_id"])
            new = UUID(row["after"]["plan_version_id"])
            owners = await uow.admin.repoint_plan_version(old, new)
            for owner in owners:
                self._forget(uow, owner["owner_type"], owner["owner_id"])
            await uow.audit.record(
                staff.id, "catalog.apply_limits", "plan_version", str(new), reason,
                {"plan_version_id": str(old), "from_audit": audit_id},
                {"plan_version_id": str(new), "subscriptions_moved": len(owners)},
            )  # fmt: skip
        return len(owners)

    async def post_settlement(
        self, staff: Staff, payment_id: UUID, fee: Paise, fee_gst: Paise, reason: str
    ) -> bool:
        reason = need_reason(reason)
        try:
            posted = await self._settlements.post_manual(payment_id, fee, fee_gst)
        except LookupError:
            raise NotFound("payment not found") from None
        async with self._uow() as uow:
            await uow.audit.record(
                staff.id, "settlement.manual", "payment", str(payment_id), reason, None,
                {"fee_paise": int(fee), "fee_gst_paise": int(fee_gst), "posted": posted},
            )  # fmt: skip
        return posted
