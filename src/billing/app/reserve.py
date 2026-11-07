"""CheckAndReserve / Release / GetEntitlements: the limit check in front of every message.

Plan mode first. When the plan refuses for a limit (or does not include the model) and the user
has a Pay as you Go wallet, the same script runs again in payg mode and the message is paid from
credits (`funded_by = "credits"`). A user whose plan *is* Pay as you Go goes straight to payg mode.
Wallet amounts are taxable paise: a hold is `taxable_of(unit price)`.
"""

from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from uuid import UUID

from opentelemetry import trace

from billing.app.entitlements import EntitlementService, ModelCatalog
from billing.app.ports import Clock, Counters, IdGenerator, ReserveArgs
from billing.domain.catalog import DenyReason, ModelGroup, PaygPrices, ReserveKind
from billing.domain.entitlements import Entitlement, GrantSource
from billing.domain.money import Paise, taxable_of
from billing.observability import (
    RELEASE_TOTAL,
    RESERVE_DOWNGRADED,
    RESERVE_DURATION,
    RESERVE_REQUESTS,
)

tracer = trace.get_tracer("billing")
NO_CREDIT_FAST_PER_DAY = 10  # LLD: only used on the credits path (Phase 11)

MESSAGE_KEYS = {
    DenyReason.DAILY_LIMIT: "limit.daily",
    DenyReason.MONTHLY_LIMIT: "limit.monthly",
    DenyReason.MODEL_NOT_IN_PLAN: "plan.model_not_included",
    DenyReason.NO_CREDIT: "credits.empty",
    DenyReason.SPEND_CAP: "credits.spend_cap",
    DenyReason.ACCOUNT_BLOCKED: "account.blocked",
}
BEST_SWITCHED_KEY = "limit.best_switched"


@dataclass(frozen=True, slots=True)
class CheckAndReserveRequest:
    user_id: UUID
    idempotency_key: str
    kind: ReserveKind
    requested_group: ModelGroup | None = None
    requested_model: str = ""


@dataclass(frozen=True, slots=True)
class CheckAndReserveResult:
    allowed: bool
    deny_reason: DenyReason | None = None
    message_key: str = ""
    hold_id: str = ""
    granted_group: ModelGroup | None = None
    downgraded: bool = False
    max_context_tokens: int = 0
    billing_account_id: UUID | None = None
    hold_expires_at_unix: int = 0
    remaining_today: int = 0
    funded_by: str = ""
    granted_model: str = ""  # only for an explicit model request


def _deny(reason: DenyReason, ent: Entitlement | None = None) -> CheckAndReserveResult:
    return CheckAndReserveResult(
        allowed=False,
        deny_reason=reason,
        message_key=MESSAGE_KEYS[reason],
        billing_account_id=ent.grant.billing_account_id if ent else None,
    )


class ReserveService:
    def __init__(
        self,
        entitlements: EntitlementService,
        catalog: ModelCatalog,
        counters: Counters,
        clock: Clock,
        ids: IdGenerator,
        *,
        hold_ttl_seconds: int = 600,
        payg: PaygPrices | None = None,
        no_credit_fast_per_day: int = NO_CREDIT_FAST_PER_DAY,
    ) -> None:
        self._ent = entitlements
        self._catalog = catalog
        self._counters = counters
        self._clock = clock
        self._ids = ids
        self._hold_ttl = hold_ttl_seconds
        self._payg = payg
        self._no_credit_fast = no_credit_fast_per_day

    # --- CheckAndReserve -----------------------------------------------------------------

    async def check_and_reserve(self, req: CheckAndReserveRequest) -> CheckAndReserveResult:
        started = perf_counter()
        plan = "unknown"
        with tracer.start_as_current_span("billing.reserve"):
            try:
                with tracer.start_as_current_span("entitlement.resolve"):
                    ent = await self._ent.resolve(req.user_id)
                plan = ent.grant.plan_version.plan_code
                result = await self._decide(req, ent)
            finally:
                RESERVE_DURATION.labels(kind=req.kind.value).observe(perf_counter() - started)
        decision = "allow" if result.allowed else "deny"
        reason = result.deny_reason.value if result.deny_reason else ""
        RESERVE_REQUESTS.labels(req.kind.value, decision, reason, plan).inc()
        if result.downgraded:
            RESERVE_DOWNGRADED.labels(plan).inc()
        return result

    async def _field_for_message(self, ent: Entitlement, req: CheckAndReserveRequest) -> str | None:
        """The counter field (a model group) this request is charged to, or None if not allowed."""
        if req.requested_model:
            group = await self._catalog.group_of(req.requested_model)
            if group is None:  # unknown model
                return None
            allowed = group in ent.allowed_groups or req.requested_model in ent.extra_models
            return group.value if allowed else None
        if req.requested_group is None:  # Auto: the plan's default group, never Best
            return ent.grant.plan_version.default_group.value
        return req.requested_group.value if req.requested_group in ent.allowed_groups else None

    async def _decide(self, req: CheckAndReserveRequest, ent: Entitlement) -> CheckAndReserveResult:
        if ent.account_blocked:
            return _deny(DenyReason.ACCOUNT_BLOCKED, ent)

        limits = ent.limits
        best_limit = -1
        if req.kind is ReserveKind.MESSAGE:
            field = await self._field_for_message(ent, req)
            if field is None:
                if ent.overflow_wallet_account_id is not None and self._payg is not None:
                    # a model the plan lacks, paid from credits
                    group = await self._group_of_request(req)
                    if group is not None:
                        return await self._reserve_credits(req, ent, group.value)
                return _deny(DenyReason.MODEL_NOT_IN_PLAN, ent)
            period, limit, best_limit = "day", limits.messages_per_day, limits.best_per_day
        elif req.kind is ReserveKind.YOUTUBE_VIDEO:
            field, period, limit = "youtube", "month", limits.youtube_per_month
        elif req.kind is ReserveKind.MANIM_VIDEO:
            field, period, limit = "manim", "day", -1  # no plan limit
        else:
            raise ValueError(f"unsupported reserve kind: {req.kind}")

        if ent.grant.source is GrantSource.PAYG and self._payg is not None:
            return await self._reserve_credits(req, ent, field)  # Pay as you Go: always credits

        now = self._clock.now()
        hold_id = str(self._ids.new())
        args = ReserveArgs(
            uid=req.user_id,
            account=ent.grant.billing_account_id,
            plan_version_id=ent.grant.plan_version.id,
            idem_key=req.idempotency_key,
            kind=req.kind,
            period=period,
            now=now,
            mode="plan",
            field=field,
            limit=limit,
            best_limit=best_limit,
            price_paise=Paise(0),
            hold_id=hold_id,
            hold_ttl=self._hold_ttl,
            no_credit_fast=NO_CREDIT_FAST_PER_DAY,
        )
        with tracer.start_as_current_span("valkey.evalsha"):
            res = await self._counters.reserve(args)

        if not res.allowed:
            assert res.deny_reason is not None
            if (
                res.deny_reason in (DenyReason.DAILY_LIMIT, DenyReason.MONTHLY_LIMIT)
                and ent.overflow_wallet_account_id is not None
                and self._payg is not None
            ):
                paid = await self._reserve_credits(req, ent, field)
                if paid.allowed or paid.deny_reason is DenyReason.SPEND_CAP:
                    return paid
            return _deny(res.deny_reason, ent)

        assert res.field is not None and res.hold_id is not None
        granted = ModelGroup(res.field) if req.kind is ReserveKind.MESSAGE else None
        return CheckAndReserveResult(
            allowed=True,
            message_key=BEST_SWITCHED_KEY if res.downgraded else "",
            hold_id=res.hold_id,
            granted_group=granted,
            downgraded=res.downgraded,
            max_context_tokens=limits.context_tokens,
            billing_account_id=ent.grant.billing_account_id,
            hold_expires_at_unix=int((now + timedelta(seconds=self._hold_ttl)).timestamp()),
            remaining_today=-1 if limit < 0 else max(0, limit - res.total),
            funded_by="plan",
            granted_model=req.requested_model if req.kind is ReserveKind.MESSAGE else "",
        )

    async def _group_of_request(self, req: CheckAndReserveRequest) -> ModelGroup | None:
        if req.requested_model:
            return await self._catalog.group_of(req.requested_model)
        return req.requested_group

    def _credit_price(self, kind: ReserveKind, field: str) -> Paise:
        """The hold for one unit paid from credits, in taxable paise."""
        assert self._payg is not None
        key = {
            ReserveKind.YOUTUBE_VIDEO: "youtube_video",
            ReserveKind.MANIM_VIDEO: "manim_video",
        }.get(kind, field)
        return taxable_of(self._payg.unit_prices[key])

    async def _reserve_credits(
        self, req: CheckAndReserveRequest, ent: Entitlement, field: str
    ) -> CheckAndReserveResult:
        """reserve.lua in payg mode against the user's wallet (`wal:{account}`)."""
        wallet = ent.overflow_wallet_account_id
        if wallet is None:
            return _deny(DenyReason.NO_CREDIT, ent)
        period = "month" if req.kind is ReserveKind.YOUTUBE_VIDEO else "day"
        now = self._clock.now()
        args = ReserveArgs(
            uid=req.user_id,
            account=wallet,
            plan_version_id=ent.grant.plan_version.id,
            idem_key=req.idempotency_key,
            kind=req.kind,
            period=period,
            now=now,
            mode="payg",
            field=field,
            limit=-1,
            best_limit=-1,
            price_paise=self._credit_price(req.kind, field),
            hold_id=str(self._ids.new()),
            hold_ttl=self._hold_ttl,
            no_credit_fast=self._no_credit_fast,
        )
        res = await self._counters.reserve(args)
        if not res.allowed:
            assert res.deny_reason is not None
            return _deny(res.deny_reason, ent)
        assert res.field is not None and res.hold_id is not None
        return CheckAndReserveResult(
            allowed=True,
            hold_id=res.hold_id,
            granted_group=ModelGroup(res.field) if req.kind is ReserveKind.MESSAGE else None,
            max_context_tokens=ent.limits.context_tokens,
            billing_account_id=wallet,
            hold_expires_at_unix=int((now + timedelta(seconds=self._hold_ttl)).timestamp()),
            remaining_today=-1,
            funded_by="credits" if res.price > 0 else "plan",
            granted_model=req.requested_model if req.kind is ReserveKind.MESSAGE else "",
        )

    # --- Release / GetEntitlements -------------------------------------------------------

    async def release(self, user_id: UUID, hold_id: str, reason: str) -> bool:
        """Give a reserved unit back. False when the hold is already gone."""
        released = await self._counters.release(user_id, hold_id, "refund")
        if released:
            RELEASE_TOTAL.labels(reason=reason or "unspecified").inc()
        return released

    async def get_entitlements(self, user_id: UUID) -> Entitlement:
        return await self._ent.resolve(user_id)

    async def allowed_models(self, ent: Entitlement) -> list[str]:
        """Every priced model in the plan's groups, plus its extra models."""
        by_model = await self._catalog.models()
        names = {m for m, g in by_model.items() if g in ent.allowed_groups} | set(ent.extra_models)
        return sorted(names)
