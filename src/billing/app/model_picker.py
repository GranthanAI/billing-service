"""What the model picker shows: the catalog of models, and what one user may use right now.

Read-only and hot: the plan and model lists are cached in process for a minute, the entitlement
comes from the entitlement cache, and today's counts are one Valkey read.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from billing.app.catalog_spec import ModelEntry
from billing.app.entitlements import EntitlementService
from billing.app.ports import Clock, Counters, UnitOfWork
from billing.domain.calendar import IST, ist_day, next_ist_midnight
from billing.domain.catalog import UNLIMITED, ModelGroup, PlanVersion


@dataclass(frozen=True, slots=True)
class _Snapshot:
    models: tuple[ModelEntry, ...]
    individual_plans: tuple[PlanVersion, ...]  # self-serve plans, lowest rank first


def _or_null(limit: int) -> int | None:
    return None if limit == UNLIMITED else limit


class ModelPicker:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        entitlements: EntitlementService,
        counters: Counters,
        clock: Clock,
        *,
        payg_enabled: bool = False,
        ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow_factory
        self._ent = entitlements
        self._counters = counters
        self._clock = clock
        self._payg = payg_enabled
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._snap: _Snapshot | None = None
        self._loaded_at = float("-inf")

    async def _snapshot(self) -> _Snapshot:
        if self._snap is None or self._monotonic() - self._loaded_at >= self._ttl:
            async with self._uow() as uow:
                models = await uow.plans.list_models()
                plans = await uow.plans.all_current()
            individual = sorted(
                (
                    r.plan
                    for r in plans
                    if r.plan.variant is None
                    and r.plan.plan_code not in ("payg",)
                    and not r.plan.plan_code.startswith("org_")
                ),
                key=lambda p: p.rank,
            )
            self._snap = _Snapshot(tuple(models), tuple(individual))
            self._loaded_at = self._monotonic()
        return self._snap

    async def all_models(self) -> list[dict[str, Any]]:
        snap = await self._snapshot()
        return [
            {
                "id": m.id,
                "label": m.label,
                "provider": m.provider,
                "group": m.group.value,
                "sort_order": m.sort_order,
            }
            for m in snap.models
        ]

    def _upgrade_to(self, group: ModelGroup, snap: _Snapshot) -> str | None:
        for plan in snap.individual_plans:
            if group in plan.allowed_groups:
                return plan.plan_code
        return "payg" if group is ModelGroup.ULTRA and self._payg else None

    async def for_user(self, user_id: UUID) -> dict[str, Any]:
        snap = await self._snapshot()
        ent = await self._ent.resolve(user_id)
        now: datetime = self._clock.now()
        counts = await self._counters.day_counts(user_id, ist_day(now))
        limits = ent.limits
        used_total = counts.get("total", 0)
        left_total = (
            None if limits.messages_per_day == UNLIMITED
            else max(0, limits.messages_per_day - used_total)
        )  # fmt: skip

        def cap(*values: int | None) -> int | None:
            real = [v for v in values if v is not None]
            return min(real) if real else None

        groups: list[dict[str, Any]] = []
        for group in ModelGroup:
            allowed = group in ent.allowed_groups
            entry: dict[str, Any] = {"group": group.value, "allowed": allowed}
            if not allowed:
                entry.update(daily_limit=0, remaining_today=0)
                entry["upgrade_to"] = self._upgrade_to(group, snap)
            elif group is ModelGroup.BEST:
                best_left = (
                    None if limits.best_per_day == UNLIMITED
                    else max(0, limits.best_per_day - counts.get("best", 0))
                )  # fmt: skip
                entry.update(
                    daily_limit=_or_null(limits.best_per_day),
                    remaining_today=cap(best_left, left_total),
                )
            else:  # the other groups share the daily total
                entry.update(
                    daily_limit=_or_null(limits.messages_per_day), remaining_today=left_total
                )
            entry["models"] = [
                {
                    "id": m.id,
                    "label": m.label,
                    "provider": m.provider,
                    "allowed": allowed or m.id in ent.extra_models,
                }
                for m in snap.models
                if m.group is group
            ]
            groups.append(entry)

        return {
            "plan": {
                "code": ent.grant.plan_version.plan_code,
                "source": ent.grant.source.value,
            },
            "default_group": ent.grant.plan_version.default_group.value,
            "messages": {
                "daily_limit": _or_null(limits.messages_per_day),
                "used_today": used_total,
                "remaining_today": left_total,
                "resets_at": next_ist_midnight(now).astimezone(IST).isoformat(),
            },
            "groups": groups,
        }
