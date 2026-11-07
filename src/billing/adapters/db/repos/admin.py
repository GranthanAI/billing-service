"""Read models for the admin console and the user's own account views (raw SQL, JSON-ready rows)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

LIVE = "('active', 'past_due', 'suspended', 'cancelling')"


def _row(r: Any) -> dict[str, Any]:
    return dict(r._mapping)


class SqlAdminRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self._c = conn

    async def _all(self, query: str, **params: object) -> list[dict[str, Any]]:
        return [_row(r) for r in (await self._c.execute(sa.text(query), params)).all()]

    # --- accounts ------------------------------------------------------------------------

    async def search_accounts(self, q: str, limit: int = 50) -> list[dict[str, Any]]:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return await self._all(
            """
            SELECT a.id, a.owner_type::text AS owner_type, a.owner_id, a.billing_email,
                   a.legal_name, a.gstin, a.state_code, a.blocked_at
              FROM billing.billing_accounts a
             WHERE a.billing_email ILIKE :like
                OR a.id::text = :q OR a.owner_id::text = :q
                OR a.gstin = upper(:q)
                OR EXISTS (SELECT 1 FROM billing.subscriptions s
                            WHERE s.billing_account_id = a.id
                              AND :q IN (s.gateway_subscription_id, s.next_gateway_subscription_id))
                OR EXISTS (SELECT 1 FROM billing.payments p
                            WHERE p.billing_account_id = a.id AND p.gateway_payment_id = :q)
                OR EXISTS (SELECT 1 FROM billing.invoices i
                            WHERE i.billing_account_id = a.id AND i.number = :q)
             ORDER BY a.created_at DESC LIMIT :n
            """,
            like=like,
            q=q,
            n=limit,
        )

    async def account_detail(
        self, account_id: UUID, month_start: datetime, month_end: datetime
    ) -> dict[str, Any] | None:
        accounts = await self._all(
            "SELECT id, owner_type::text AS owner_type, owner_id, billing_email, legal_name, "
            "gstin, state_code, blocked_at, blocked_reason, created_at "
            "FROM billing.billing_accounts WHERE id = :a",
            a=account_id,
        )
        if not accounts:
            return None
        subs = await self._all(
            "SELECT s.id, s.status::text AS status, s.interval::text AS interval, s.seats, "
            "pv.plan_code, pv.version, s.current_period_start, s.current_period_end, "
            "s.grace_until, s.gateway_subscription_id, s.next_gateway_subscription_id, "
            "s.lock_version "
            "FROM billing.subscriptions s JOIN billing.plan_versions pv "
            "ON pv.id = s.plan_version_id WHERE s.billing_account_id = :a "
            "ORDER BY s.created_at DESC",
            a=account_id,
        )
        history = await self._all(
            "SELECT h.from_status::text AS from_status, h.to_status::text AS to_status, "
            "h.trigger, h.cause_ref, h.at FROM billing.subscription_history h "
            "JOIN billing.subscriptions s ON s.id = h.subscription_id "
            "WHERE s.billing_account_id = :a ORDER BY h.id DESC LIMIT 100",
            a=account_id,
        )
        payments = await self._all(
            "SELECT id, gateway_payment_id, purpose, method, amount_paise, status, captured_at "
            "FROM billing.payments WHERE billing_account_id = :a "
            "ORDER BY captured_at DESC LIMIT 100",
            a=account_id,
        )
        invoices = await self._all(
            "SELECT id, number, kind, total_paise, status, issued_at "
            "FROM billing.invoices WHERE billing_account_id = :a ORDER BY issued_at DESC LIMIT 100",
            a=account_id,
        )
        cost = await self._all(
            "SELECT coalesce(sum(cost_micro_inr), 0)::bigint AS cost_micro_inr, count(*) AS events "
            "FROM billing.usage_events WHERE billing_account_id = :a "
            "AND occurred_at >= :s AND occurred_at < :e",
            a=account_id,
            s=month_start,
            e=month_end,
        )
        revenue = await self._all(
            "SELECT coalesce(sum(CASE WHEN kind = 'credit_note' THEN -taxable_paise "
            "ELSE taxable_paise END), 0)::bigint AS revenue_paise "
            "FROM billing.invoices WHERE billing_account_id = :a "
            "AND issued_at >= :s AND issued_at < :e",
            a=account_id,
            s=month_start,
            e=month_end,
        )
        cost_micro = int(cost[0]["cost_micro_inr"])
        revenue_paise = int(revenue[0]["revenue_paise"])
        return {
            "account": accounts[0],
            "subscriptions": subs,
            "history": history,
            "payments": payments,
            "invoices": invoices,
            "this_month": {
                "usage_events": int(cost[0]["events"]),
                "cost_micro_inr": cost_micro,
                "revenue_paise": revenue_paise,  # taxable value, credit notes netted off
                "margin_micro_inr": revenue_paise * 10_000 - cost_micro,  # 1 paisa = 10,000 µINR
            },
        }

    # --- costs and reconciliation ---------------------------------------------------------

    async def costs(self, start: datetime, end: datetime, by: str) -> list[dict[str, Any]]:
        window = {"s": start, "e": end}
        if by == "plan":
            return await self._all(
                "SELECT pv.plan_code AS key, sum(ue.cost_micro_inr)::bigint AS cost_micro_inr, "
                "count(*) AS events, sum(ue.price_paise)::bigint AS price_paise "
                "FROM billing.usage_events ue JOIN billing.plan_versions pv "
                "ON pv.id = ue.plan_version_id WHERE ue.occurred_at >= :s AND ue.occurred_at < :e "
                "GROUP BY 1 ORDER BY 2 DESC",
                **window,
            )
        if by == "user":
            return await self._all(
                "SELECT user_id::text AS key, sum(cost_micro_inr)::bigint AS cost_micro_inr, "
                "count(*) AS events, sum(price_paise)::bigint AS price_paise "
                "FROM billing.usage_events WHERE occurred_at >= :s AND occurred_at < :e "
                "GROUP BY user_id ORDER BY 2 DESC LIMIT 20",
                **window,
            )
        return await self.provider_costs_between(start, end)

    async def provider_costs_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Cost per provider; events recorded before the breakdown existed fall back to the
        provider of the model that answered."""
        return await self._all(
            """
            WITH r AS (SELECT * FROM billing.usage_events
                        WHERE occurred_at >= :s AND occurred_at < :e)
            SELECT key, sum(v)::bigint AS cost_micro_inr, count(*) AS events FROM (
              SELECT kv.key AS key, kv.value::bigint AS v
                FROM r, LATERAL jsonb_each_text(r.provider_costs) kv
               WHERE r.provider_costs IS NOT NULL
              UNION ALL
              SELECT coalesce(m.provider, 'unknown'), r.cost_micro_inr
                FROM r LEFT JOIN billing.models m ON m.id = r.model
               WHERE r.provider_costs IS NULL
            ) x GROUP BY key ORDER BY 2 DESC
            """,
            s=start,
            e=end,
        )

    async def latest_reconciliation(self, run_date: date) -> list[dict[str, Any]]:
        return await self._all(
            'SELECT DISTINCT ON ("check") "check", status, difference_paise, details, created_at '
            'FROM billing.reconciliation_runs WHERE run_date = :d ORDER BY "check", id DESC',
            d=run_date,
        )

    # --- the user's own views --------------------------------------------------------------

    async def daily_usage(self, user_id: UUID, start: date, end: date) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT day, model_group::text AS model_group, messages, billable_units "
            "FROM billing.usage_daily WHERE user_id = :u AND day >= :f AND day <= :t "
            "ORDER BY day, model_group",
            u=user_id,
            f=start,
            t=end,
        )

    # --- catalog changes ---------------------------------------------------------------------

    async def audit_row(self, audit_id: int) -> dict[str, Any] | None:
        rows = await self._all(
            "SELECT id, action, target_type, target_id, before, after "
            "FROM billing.admin_audit WHERE id = :i",
            i=audit_id,
        )
        return rows[0] if rows else None

    async def repoint_plan_version(self, old: UUID, new: UUID) -> list[dict[str, Any]]:
        """Move live subscriptions from `old` to `new` (a limits-only change: same prices).
        Returns the owners whose entitlement cache must be cleared."""
        moved = await self._all(
            f"UPDATE billing.subscriptions SET plan_version_id = :new, "  # noqa: S608
            f"lock_version = lock_version + 1, updated_at = now() "
            f"WHERE plan_version_id = :old AND status IN {LIVE} RETURNING billing_account_id",
            old=old,
            new=new,
        )
        await self._c.execute(
            sa.text(
                "UPDATE billing.subscriptions SET pending_plan_version_id = :new "
                "WHERE pending_plan_version_id = :old"
            ),
            {"old": old, "new": new},
        )
        ids = [m["billing_account_id"] for m in moved]
        if not ids:
            return []
        return await self._all(
            "SELECT owner_type::text AS owner_type, owner_id FROM billing.billing_accounts "
            "WHERE id = ANY(:ids)",
            ids=ids,
        )

    async def gauge_counts(
        self,
    ) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
        inbox = {
            r["status"]: int(r["n"])
            for r in await self._all(
                "SELECT status, count(*) AS n FROM billing.webhook_inbox GROUP BY status"
            )
        }
        subs = {
            (r["plan_code"], r["status"]): int(r["n"])
            for r in await self._all(
                "SELECT pv.plan_code, s.status::text AS status, count(*) AS n "
                "FROM billing.subscriptions s JOIN billing.plan_versions pv "
                "ON pv.id = s.plan_version_id GROUP BY 1, 2"
            )
        }
        return inbox, subs

    # --- reconciliation ------------------------------------------------------------------------

    async def payments_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT gateway_payment_id, amount_paise FROM billing.payments "
            "WHERE captured_at >= :s AND captured_at < :e "
            "AND method IS DISTINCT FROM 'bank_transfer'",  # never went through Razorpay
            s=start,
            e=end,
        )

    async def clearing_charged_between(self, start: datetime, end: datetime) -> int:
        """What the ledger says was charged to customers: debits to the clearing account by
        charge and top-up transactions."""
        rows = await self._all(
            "SELECT coalesce(sum(e.amount_paise), 0)::bigint AS total "
            "FROM billing.ledger_entries e "
            "JOIN billing.ledger_transactions t ON t.id = e.transaction_id "
            "JOIN billing.ledger_accounts a ON a.id = e.account_id "
            "WHERE a.code = 'asset:razorpay_clearing' AND e.amount_paise > 0 "
            "AND t.kind IN ('subscription_charge', 'wallet_topup') "
            "AND t.occurred_at >= :s AND t.occurred_at < :e",
            s=start,
            e=end,
        )
        return int(rows[0]["total"])

    async def ledger_integrity(self) -> dict[str, Any]:
        unbalanced = await self._all(
            "SELECT transaction_id::text AS transaction_id, sum(amount_paise)::bigint AS total "
            "FROM billing.ledger_entries GROUP BY transaction_id HAVING sum(amount_paise) <> 0 "
            "LIMIT 100"
        )
        mismatched = await self._all(
            "SELECT a.code, coalesce(b.balance_paise, 0)::bigint AS balance, "
            "coalesce(e.total, 0)::bigint AS entries "
            "FROM billing.ledger_accounts a "
            "LEFT JOIN billing.ledger_balances b ON b.account_id = a.id "
            "LEFT JOIN (SELECT account_id, sum(amount_paise) AS total FROM billing.ledger_entries "
            "GROUP BY account_id) e ON e.account_id = a.id "
            "WHERE coalesce(b.balance_paise, 0) <> coalesce(e.total, 0) ORDER BY a.code LIMIT 100"
        )
        return {"unbalanced": unbalanced, "balance_mismatches": mismatched}

    async def imported_provider_costs(self, day: date) -> dict[str, int]:
        rows = await self._all(
            "SELECT provider, cost_micro_inr FROM billing.provider_costs WHERE day = :d", d=day
        )
        return {r["provider"]: int(r["cost_micro_inr"]) for r in rows}

    async def upsert_provider_cost(
        self, provider: str, day: date, cost_micro_inr: int, source: str
    ) -> None:
        await self._c.execute(
            sa.text(
                "INSERT INTO billing.provider_costs (provider, day, cost_micro_inr, source) "
                "VALUES (:p, :d, :c, :s) ON CONFLICT (provider, day) DO UPDATE SET "
                "cost_micro_inr = EXCLUDED.cost_micro_inr, source = EXCLUDED.source, "
                "imported_at = now()"
            ),
            {"p": provider, "d": day, "c": cost_micro_inr, "s": source},
        )

    async def record_reconciliation(
        self, run_date: date, check: str, status: str, difference: int, details: dict[str, Any]
    ) -> None:
        import json

        await self._c.execute(
            sa.text(
                'INSERT INTO billing.reconciliation_runs (run_date, "check", status, '
                "difference_paise, details) VALUES (:d, :c, :s, :x, CAST(:j AS jsonb))"
            ),
            {"d": run_date, "c": check, "s": status, "x": difference, "j": json.dumps(details)},
        )
