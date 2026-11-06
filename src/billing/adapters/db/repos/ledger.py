from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import (
    ledger_accounts,
    ledger_balances,
    ledger_entries,
    ledger_transactions,
)
from billing.app.ports import IdGenerator
from billing.domain.ledger import LedgerTransaction, assert_balanced

KINDS = frozenset({"asset", "liability", "revenue", "expense"})


class SqlLedgerRepo:
    def __init__(self, conn: AsyncConnection, ids: IdGenerator) -> None:
        self._c = conn
        self._ids = ids

    async def post(
        self, tx: LedgerTransaction, ref_type: str, ref_id: str, occurred_at: datetime
    ) -> bool:
        assert_balanced(tx)
        inserted = await self._c.execute(
            pg_insert(ledger_transactions)
            .values(
                id=self._ids.new(),
                kind=tx.kind.value,
                ref_type=ref_type,
                ref_id=ref_id,
                idempotency_key=tx.idempotency_key,
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(ledger_transactions.c.id)
        )
        row = inserted.first()
        if row is None:
            return False
        tx_id = row[0]

        # Accounts are created lazily (wallets); codes sorted so concurrent posts lock in order.
        codes = sorted({p.account_code for p in tx.postings})
        for code in codes:
            kind = code.split(":", 1)[0]
            if kind not in KINDS:
                raise ValueError(f"unknown ledger account kind in {code!r}")
            await self._c.execute(
                pg_insert(ledger_accounts)
                .values(id=self._ids.new(), code=code, kind=kind)
                .on_conflict_do_nothing(index_elements=["code"])
            )
        rows = await self._c.execute(
            sa.select(ledger_accounts.c.id, ledger_accounts.c.code).where(
                ledger_accounts.c.code.in_(codes)
            )
        )
        account_id = {r.code: r.id for r in rows}

        entries = await self._c.execute(
            sa.insert(ledger_entries).returning(
                ledger_entries.c.id, ledger_entries.c.account_id, ledger_entries.c.amount_paise
            ),
            [
                {
                    "transaction_id": tx_id,
                    "account_id": account_id[p.account_code],
                    "amount_paise": p.amount,
                }
                for p in tx.postings
            ],
        )
        delta: dict[object, int] = {}
        last: dict[object, int] = {}
        for entry_id, acct, amount in entries.all():
            delta[acct] = delta.get(acct, 0) + amount
            last[acct] = max(last.get(acct, 0), entry_id)

        for code in codes:  # same sorted order everywhere
            acct = account_id[code]
            ins = pg_insert(ledger_balances).values(
                account_id=acct, balance_paise=delta[acct], last_entry_id=last[acct]
            )
            await self._c.execute(
                ins.on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={
                        "balance_paise": ledger_balances.c.balance_paise
                        + ins.excluded.balance_paise,
                        "last_entry_id": sa.func.greatest(
                            ledger_balances.c.last_entry_id, ins.excluded.last_entry_id
                        ),
                    },
                )
            )
        return True

    async def _balance(self, code: str, lock: bool) -> int:
        q = (
            sa.select(ledger_balances.c.balance_paise)
            .select_from(
                ledger_balances.join(
                    ledger_accounts, ledger_accounts.c.id == ledger_balances.c.account_id
                )
            )
            .where(ledger_accounts.c.code == code)
        )
        if lock:
            q = q.with_for_update(of=ledger_balances)
        value = (await self._c.execute(q)).scalar_one_or_none()
        return int(value) if value is not None else 0

    async def recognised_for_payment(self, payment_id: UUID) -> int:
        result = await self._c.execute(
            sa.text(
                "SELECT coalesce(sum(e.amount_paise), 0)::bigint FROM billing.ledger_entries e "
                "JOIN billing.ledger_transactions t ON t.id = e.transaction_id "
                "JOIN billing.ledger_accounts a ON a.id = e.account_id "
                "WHERE a.code = 'liability:deferred_revenue' AND t.kind = 'revenue_recognition' "
                "AND (t.idempotency_key LIKE :slices OR t.idempotency_key = :release)"
            ),
            {"slices": f"recognise:%:{payment_id}:%", "release": f"release:{payment_id}"},
        )
        return int(result.scalar_one())

    async def balance(self, code: str) -> int:
        return await self._balance(code, lock=False)

    async def lock_balance(self, code: str) -> int:
        return await self._balance(code, lock=True)
