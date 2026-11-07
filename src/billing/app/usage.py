"""Applying usage: every answered message, Manim render and YouTube ingest is rated and recorded
exactly once, and its hold is settled.

Order for one event: claim `processed_events`, insert `usage_events` (both in one transaction),
then, after the commit, settle the hold in Valkey (`commit.lua` or release) and raise threshold
events. Replays are harmless at every step: the claim, the insert and `commit.lua`'s done marker
all deduplicate, and a replay still runs the Valkey step so a crash between the database commit
and Valkey heals itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import structlog
from opentelemetry import trace

from billing.app.entitlements import EntitlementService
from billing.app.events import UsageThresholdReachedEvent
from billing.app.ports import Clock, Counters, Hold, UnitOfWork
from billing.app.records import UsageEventRecord
from billing.domain.calendar import IST, ist_day, next_ist_midnight, require_aware
from billing.domain.catalog import UNLIMITED, ModelGroup, OwnerType, PaygPrices, ReserveKind
from billing.domain.entitlements import Entitlement
from billing.domain.money import MicroInr, Paise
from billing.domain.rating import (
    PURPOSES,
    LlmCall,
    cost_micro_inr,
    inr_to_micro_inr,
    rate,
    usd_to_micro_inr,
)
from billing.observability import USAGE_COST, USAGE_EVENTS, USAGE_LATE_COMMIT

log = structlog.get_logger()
tracer = trace.get_tracer("billing")

CONSUMER = "billing-usage"
THRESHOLD_PERCENTS = (80, 100)
KIND_NAMES = {
    ReserveKind.MESSAGE: "message",
    ReserveKind.YOUTUBE_VIDEO: "youtube_video",
    ReserveKind.MANIM_VIDEO: "manim_video",
}
# Plan mode never reads PaygPrices; credits mode gets the catalog's.
NO_PAYG = PaygPrices(unit_prices={})


class NoGroupForUsage(ValueError):
    """A message event carried no model group and its model is not in the catalog."""


@dataclass(frozen=True, slots=True)
class UsageCall:
    provider: str
    model: str
    purpose: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """What the three usage topics have in common, after the adapter has parsed them."""

    kind: ReserveKind
    source_id: str  # request_message_id, job_id or source_id
    dedupe_key: str
    user_id: UUID
    occurred_at: datetime
    status: Literal["success", "error", "cancelled"]
    hold_id: str | None = None
    model_group: str | None = None
    model: str | None = None
    calls: tuple[UsageCall, ...] = ()


@dataclass(frozen=True, slots=True)
class Recorded:
    account_id: UUID
    plan_code: str
    provider_costs: dict[tuple[str, str], int]  # (provider, model_group) -> micro-rupees
    success: bool
    charged: int = 0  # taxable paise taken from the wallet (credits mode)


def effective_status(ev: UsageEvent) -> Literal["success", "error"]:
    """`cancelled` is a success when the user saw some of the answer, else an error."""
    if ev.status == "cancelled":
        return "success" if any(c.output_tokens > 0 for c in ev.calls) else "error"
    return ev.status


def _domain_calls(calls: tuple[UsageCall, ...]) -> tuple[list[LlmCall], UsageCall | None]:
    """The last answer call sets the units; earlier answers (llm-service retries) and every
    other call add to the cost only. Unknown purposes are cost-only too."""
    answers = [i for i, c in enumerate(calls) if c.purpose == "answer"]
    primary_index = answers[-1] if answers else None
    out: list[LlmCall] = []
    for i, c in enumerate(calls):
        purpose = "answer" if i == primary_index else c.purpose
        if purpose == "answer" and i != primary_index or purpose not in PURPOSES:
            purpose = "request_analysis"
        cached = min(c.cached_input_tokens, c.input_tokens)
        out.append(
            LlmCall(
                c.provider,
                c.model,
                purpose,
                max(0, c.input_tokens),
                max(0, cached),
                max(0, c.output_tokens),
            )  # fmt: skip
        )
    return out, calls[primary_index] if primary_index is not None else None


class UsageService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        entitlements: EntitlementService,
        counters: Counters,
        clock: Clock,
        *,
        manim_render_cost_usd: Decimal = Decimal("0.01"),
        youtube_ingest_cost_inr: Decimal = Decimal("0.30"),
        payg: PaygPrices = NO_PAYG,
    ) -> None:
        self._uow = uow_factory
        self._ent = entitlements
        self._counters = counters
        self._clock = clock
        self._manim_usd = manim_render_cost_usd
        self._youtube_inr = youtube_ingest_cost_inr
        self._payg = payg

    # --- entry point -----------------------------------------------------------------------

    async def apply(self, ev: UsageEvent) -> Literal["applied", "duplicate"]:
        with tracer.start_as_current_span("billing.usage.apply") as span:
            span.set_attribute("usage.kind", KIND_NAMES[ev.kind])
            return await self._apply(ev)

    async def _apply(self, ev: UsageEvent) -> Literal["applied", "duplicate"]:
        require_aware(ev.occurred_at)
        hold = await self._counters.peek_hold(ev.user_id, ev.hold_id) if ev.hold_id else None
        ent = None if hold is not None else await self._ent.resolve(ev.user_id)
        success = effective_status(ev) == "success"

        async with self._uow() as uow:
            claimed = await uow.processed.claim(CONSUMER, ev.dedupe_key)
            recorded = await self._record(uow, ev, hold, ent, success) if claimed else None

        await self._settle(ev, hold, success, recorded.charged if recorded else 0)
        result: Literal["applied", "duplicate"] = "applied" if recorded else "duplicate"
        USAGE_EVENTS.labels(kind=KIND_NAMES[ev.kind], result=result).inc()
        if recorded is not None:
            for (provider, group), cost in recorded.provider_costs.items():
                USAGE_COST.labels(
                    provider=provider, model_group=group, plan=recorded.plan_code
                ).inc(cost)
            if success:
                await self._thresholds(ev, recorded)
        return result

    # --- recording -------------------------------------------------------------------------

    async def _record(
        self,
        uow: UnitOfWork,
        ev: UsageEvent,
        hold: Hold | None,
        ent: Entitlement | None,
        success: bool,
    ) -> Recorded:
        if hold is not None:
            account_id, plan_version_id = hold.billing_account_id, hold.plan_version_id
        else:
            assert ent is not None
            grant = ent.grant
            account_id, plan_version_id = grant.billing_account_id, grant.plan_version.id
        if account_id is None:  # a Free user's account is created on first usage, without email
            account_id = (await uow.accounts.get_or_create(OwnerType.USER, ev.user_id)).id
        plan = await uow.plans.by_id(plan_version_id)
        plan_code = plan.plan.plan_code if plan else "unknown"
        sub = await uow.subscriptions.live_for_account(account_id)

        book = await uow.pricing.price_book_at(ev.occurred_at)
        fx = await uow.pricing.fx_at(ev.occurred_at)
        domain_calls, primary = _domain_calls(ev.calls)

        group: ModelGroup | None = None
        if ev.kind is ReserveKind.MESSAGE:
            group = hold.group if hold is not None and hold.group else None
            if group is None and ev.model_group:
                group = ModelGroup(ev.model_group)
            if group is None and primary is not None:
                found = (await uow.pricing.model_groups_at(ev.occurred_at)).get(primary.model)
                group = found
            if group is None:
                raise NoGroupForUsage(f"no model group for message {ev.source_id}")

        calls_cost = cost_micro_inr(domain_calls, book, fx)
        units, price = 0, Paise(0)
        # paid from credits: the hold was taken in payg mode with a price (a free Fast message
        # has price 0 and costs the user nothing)
        credits = hold is not None and hold.mode == "payg" and hold.price > 0 and success
        if success:
            if ev.kind is ReserveKind.MESSAGE:
                if primary is None:
                    raise ValueError(f"message {ev.source_id} succeeded with no answer call")
                assert group is not None
                rating = rate(
                    domain_calls,
                    book,
                    fx,
                    group,
                    "payg" if credits else "plan",
                    self._payg if credits else NO_PAYG,
                )
                cost, units, price = rating.cost, rating.billable_units, rating.price
            elif ev.kind is ReserveKind.MANIM_VIDEO:
                cost = MicroInr(calls_cost + usd_to_micro_inr(self._manim_usd, fx))
                units = 1
                if credits:
                    price = self._payg.unit_prices["manim_video"]
            else:
                cost = MicroInr(calls_cost + inr_to_micro_inr(self._youtube_inr))
                units = 1
                if credits:
                    price = self._payg.unit_prices["youtube_video"]
        else:
            cost = calls_cost  # the provider charged for what was attempted

        per_provider: dict[str, int] = {}
        for c, d in zip(ev.calls, domain_calls, strict=True):
            per_provider[c.provider] = per_provider.get(c.provider, 0) + int(
                cost_micro_inr([d], book, fx)
            )
        extra = int(cost) - sum(per_provider.values())  # rounding, or a render/ingest estimate
        if extra:
            owner = (
                "internal"
                if ev.kind is not ReserveKind.MESSAGE
                else next(iter(per_provider), "internal")
            )
            per_provider[owner] = per_provider.get(owner, 0) + extra

        aux_in = aux_out = 0
        for c in ev.calls:
            if c is not primary:
                aux_in += max(0, c.input_tokens)
                aux_out += max(0, c.output_tokens)
        await uow.usage.insert(
            UsageEventRecord(
                kind=KIND_NAMES[ev.kind],
                source_id=ev.source_id,
                occurred_at=ev.occurred_at,
                user_id=ev.user_id,
                billing_account_id=account_id,
                plan_version_id=plan_version_id,
                billable_units=units,
                cost_micro_inr=cost,
                fx_usd_inr=fx,
                subscription_id=sub.id if sub else None,
                model=primary.model if primary else ev.model,
                model_group=group,
                input_tokens=primary.input_tokens if primary else 0,
                cached_input_tokens=min(primary.cached_input_tokens, primary.input_tokens)
                if primary
                else 0,
                output_tokens=primary.output_tokens if primary else 0,
                aux_input_tokens=aux_in,
                aux_output_tokens=aux_out,
                price_paise=price,
                hold_id=ev.hold_id,
                provider_costs=per_provider,
            )
        )

        charged = 0
        if credits and price > 0:
            charged = await uow.wallets.debit(
                account_id, price, f"usage:{ev.source_id}", self._clock.now()
            )

        label = group.value if group else KIND_NAMES[ev.kind]
        by_provider = {(provider, label): value for provider, value in per_provider.items()}
        return Recorded(account_id, plan_code, by_provider, success, charged)

    # --- settling the hold ------------------------------------------------------------------

    async def _settle(
        self, ev: UsageEvent, hold: Hold | None, success: bool, charged: int = 0
    ) -> None:
        if not success:
            if ev.hold_id:
                await self._counters.release(ev.user_id, ev.hold_id, "refund")
            return
        if ev.kind is ReserveKind.MESSAGE:
            field = hold.group.value if hold is not None and hold.group else (ev.model_group or "")
        else:
            field = "youtube" if ev.kind is ReserveKind.YOUTUBE_VIDEO else "manim"
        if not field and ev.kind is ReserveKind.MESSAGE:
            field = await self._group_from_model(ev) or "fast"
        result = await self._counters.commit(
            ev.user_id,
            ev.hold_id or "none",
            Paise(charged),
            ev.source_id,
            ev.kind,
            field,
            ev.occurred_at,
        )
        if result == "OK" and hold is None:
            USAGE_LATE_COMMIT.inc()  # the hold had expired (or never existed): counted now

    async def _group_from_model(self, ev: UsageEvent) -> str | None:
        if not ev.model:
            return None
        async with self._uow() as uow:
            found = (await uow.pricing.model_groups_at(ev.occurred_at)).get(ev.model)
        return found.value if found else None

    # --- threshold events -------------------------------------------------------------------

    async def _thresholds(self, ev: UsageEvent, rec: Recorded) -> None:
        """80% and 100% of a limit, once per limit, user and period (a Valkey SET NX guard).
        A crash between the guard and the outbox write loses that one alert; accepted."""
        ent = await self._ent.resolve(ev.user_id)
        limits = ent.limits
        checks: list[tuple[str, int, int, str, datetime]] = []
        if ev.kind is ReserveKind.MESSAGE:
            counts = await self._counters.day_counts(ev.user_id, ist_day(ev.occurred_at))
            period, resets = ist_day(ev.occurred_at).strftime("%Y%m%d"), next_ist_midnight(
                ev.occurred_at
            )  # fmt: skip
            checks.append(
                (
                    "messages_per_day",
                    counts.get("total", 0),
                    limits.messages_per_day,
                    period,
                    resets,
                )
            )
            if limits.best_per_day > 0:
                checks.append(
                    ("best_per_day", counts.get("best", 0), limits.best_per_day, period, resets)
                )
        elif ev.kind is ReserveKind.YOUTUBE_VIDEO:
            counts = await self._counters.month_counts(ev.user_id, ev.occurred_at)
            local = ev.occurred_at.astimezone(IST)
            nxt = (local.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            checks.append(
                ("youtube_per_month", counts.get("youtube", 0), limits.youtube_per_month,
                 local.strftime("%Y%m"), nxt.replace(second=0, microsecond=0))
            )  # fmt: skip
        fired: list[UsageThresholdReachedEvent] = []
        for limit, used, allowed, period, resets in checks:
            if allowed == UNLIMITED or allowed <= 0:
                continue
            for percent in THRESHOLD_PERCENTS:
                if used * 100 < percent * allowed:
                    continue
                ttl = int((resets - self._clock.now()).total_seconds()) + 3600
                key = f"thr:{{{ev.user_id}}}:{limit}:{period}:{percent}"
                if await self._counters.set_once(key, ttl):
                    fired.append(
                        UsageThresholdReachedEvent(
                            ev.user_id, rec.account_id, limit, percent, used, allowed, resets
                        )
                    )
        if fired:
            async with self._uow() as uow:
                for event in fired:
                    await uow.publisher.enqueue(event)
