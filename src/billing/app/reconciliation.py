"""reconcile.daily: four checks on one IST day. Reports only; it never corrects anything.

Each run writes one `reconciliation_runs` row per check and sets
`billing_reconciliation_difference_paise{check}`. Any difference pages (alert rule).
"""

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from billing.app.ports import Clock, Counters, PaymentGateway, UnitOfWork
from billing.domain.calendar import IST
from billing.domain.rating import inr_to_micro_inr
from billing.observability import RECON_DIFFERENCE

log = structlog.get_logger()

PROVIDER_TOLERANCE = Decimal("0.05")  # more than 5% apart is flagged
COUNTER_TTL_DAYS = 2  # day counters live 48 hours
CHECKS = ("razorpay_payments", "ledger", "provider_cost", "valkey_counters")


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: str
    status: str  # ok | difference | error
    difference: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def ist_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=IST)
    return start, start + timedelta(days=1)


def parse_cost_csv(text: str) -> list[tuple[date, int]]:
    """`day,cost_inr` rows to (day, micro-rupees). Strict: one bad row rejects the file."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or {"day", "cost_inr"} - set(reader.fieldnames):
        raise ValueError("the CSV needs the columns day,cost_inr")
    rows: dict[date, int] = {}
    for n, row in enumerate(reader, start=2):
        try:
            day = date.fromisoformat((row["day"] or "").strip())
            cost = Decimal((row["cost_inr"] or "").strip())
        except (ValueError, InvalidOperation):
            raise ValueError(f"line {n}: not a valid day,cost_inr row") from None
        if cost < 0:
            raise ValueError(f"line {n}: cost must not be negative")
        if day in rows:
            raise ValueError(f"line {n}: {day} appears twice")
        rows[day] = int(inr_to_micro_inr(cost))
    return sorted(rows.items())


class ReconciliationService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        gateway: PaymentGateway,
        counters: Counters,
        clock: Clock,
    ) -> None:
        self._uow = uow_factory
        self._gateway = gateway
        self._counters = counters
        self._clock = clock

    async def run(self, day: date) -> list[CheckResult]:
        results = []
        for name, check in (
            ("razorpay_payments", self._payments),
            ("ledger", self._ledger),
            ("provider_cost", self._provider_cost),
            ("valkey_counters", self._counters_check),
        ):
            try:
                result = await check(day)
            except Exception as exc:  # a check that cannot run must be visible, not silent
                log.exception("reconciliation_check_failed", check=name)
                result = CheckResult(name, "error", 0, {"error": f"{type(exc).__name__}: {exc}"})
            results.append(result)
        async with self._uow() as uow:
            for r in results:
                await uow.admin.record_reconciliation(
                    day, r.check, r.status, r.difference, r.details
                )
        for r in results:
            RECON_DIFFERENCE.labels(check=r.check).set(r.difference)
            if r.status != "ok":
                log.error("reconciliation_difference", check=r.check, status=r.status, **r.details)
        return results

    # 1. Razorpay's captured payments against our payments and the clearing account ----------------

    async def _payments(self, day: date) -> CheckResult:
        start, end = ist_window(day)
        theirs = await self._gateway.list_payments(start, end)  # outside any transaction
        async with self._uow() as uow:
            ours = await uow.admin.payments_between(start, end)
            ledger_total = await uow.admin.clearing_charged_between(start, end)
        their_amounts = {p.id: int(p.amount) for p in theirs}
        our_amounts = {r["gateway_payment_id"]: int(r["amount_paise"]) for r in ours}
        only_theirs = sorted(set(their_amounts) - set(our_amounts))
        only_ours = sorted(set(our_amounts) - set(their_amounts))
        amount_differs = sorted(
            i for i in set(their_amounts) & set(our_amounts) if their_amounts[i] != our_amounts[i]
        )
        their_total, our_total = sum(their_amounts.values()), sum(our_amounts.values())
        difference = their_total - our_total
        if difference == 0:
            difference = our_total - ledger_total
        details = {
            "razorpay_total": their_total,
            "payments_total": our_total,
            "ledger_clearing_total": ledger_total,
            "only_in_razorpay": only_theirs[:50],
            "only_in_payments": only_ours[:50],
            "amount_differs": amount_differs[:50],
        }
        clean = (
            not only_theirs
            and not only_ours
            and not amount_differs
            and their_total == our_total == ledger_total
        )
        return CheckResult(
            "razorpay_payments", "ok" if clean else "difference", difference, details
        )

    # 2. every transaction sums to zero, and balances equal the sum of their entries

    async def _ledger(self, day: date) -> CheckResult:
        async with self._uow() as uow:
            found = await uow.admin.ledger_integrity()
        unbalanced, mismatches = found["unbalanced"], found["balance_mismatches"]
        difference = sum(abs(int(u["total"])) for u in unbalanced) + sum(
            abs(int(m["balance"]) - int(m["entries"])) for m in mismatches
        )
        clean = not unbalanced and not mismatches
        return CheckResult(
            "ledger", "ok" if clean else "difference", difference,
            {"unbalanced_transactions": unbalanced, "balance_mismatches": mismatches},
        )  # fmt: skip

    # 3. the providers invoices against what we rated

    async def _provider_cost(self, day: date) -> CheckResult:
        start, end = ist_window(day)
        async with self._uow() as uow:
            imported = await uow.admin.imported_provider_costs(day)
            rated_rows = await uow.admin.provider_costs_between(start, end)
        if not imported:
            return CheckResult("provider_cost", "ok", 0, {"skipped": "no provider export imported"})
        rated = {r["key"]: int(r["cost_micro_inr"]) for r in rated_rows}
        flagged: dict[str, dict[str, Any]] = {}
        difference = 0
        for provider in sorted(set(imported) | set(rated)):
            theirs, ours = imported.get(provider), rated.get(provider, 0)
            if theirs is None:
                continue  # a provider we have no export for is not checked
            gap = abs(theirs - ours)
            ratio = Decimal(gap) / Decimal(max(theirs, ours, 1))
            if ratio > PROVIDER_TOLERANCE:
                flagged[provider] = {
                    "imported": theirs,
                    "rated": ours,
                    "percent": float(ratio * 100),
                }
                difference += round(gap / 10_000)  # micro-rupees to paise
        details = {"checked": sorted(imported), "flagged": flagged, "tolerance_percent": 5}
        return CheckResult("provider_cost", "difference" if flagged else "ok", difference, details)

    # 4. Valkey day counters against usage_events

    async def _counters_check(self, day: date) -> CheckResult:
        today = self._clock.now().astimezone(IST).date()
        if (today - day).days > COUNTER_TTL_DAYS:
            return CheckResult("valkey_counters", "ok", 0, {"skipped": "counters have expired"})
        async with self._uow() as uow:
            expected = await uow.usage.counts_by_user_for_day(day)
        actual = await self._counters.day_counts_all(day)
        off: dict[str, dict[str, int]] = {}
        difference = 0
        for user in sorted(set(expected) | set(actual), key=str):
            want = {k: v for k, v in expected.get(user, {}).items() if v}
            got = {k: v for k, v in actual.get(user, {}).items() if k != "total" and v}
            if want != got:
                fields = set(want) | set(got)
                delta = sum(abs(want.get(f, 0) - got.get(f, 0)) for f in fields)
                difference += delta
                off[str(user)] = {f: got.get(f, 0) - want.get(f, 0) for f in sorted(fields)}
        details = {
            "users_checked": len(set(expected) | set(actual)),
            "off": dict(list(off.items())[:50]),
        }
        return CheckResult("valkey_counters", "difference" if off else "ok", difference, details)
