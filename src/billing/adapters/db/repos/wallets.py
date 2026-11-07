"""Wallets and credit lots. A wallet holds the taxable value of what was paid (the ledger account
is a liability, so its balance is negative); credit lots say which top-up it came from and when
it expires. Spending takes the lot that expires first."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from billing.adapters.db.tables import credit_lots, ledger_accounts, ledger_balances, wallets
from billing.app.ports import IdGenerator, LedgerRepo
from billing.app.records import CreditLot, WalletRecord
from billing.domain.ledger import credit_expiry, wallet_code, wallet_debit
from billing.domain.money import Paise, taxable_of


def _wallet(r: Any) -> WalletRecord:
    cap = r.monthly_cap_paise
    return WalletRecord(
        r.account_id, r.ledger_account_id, Paise(cap) if cap is not None else None, r.created_at
    )


def _lot(r: Any) -> CreditLot:
    return CreditLot(
        r.id,
        r.wallet_account_id,
        r.payment_id,
        Paise(r.amount_paise),
        Paise(r.remaining_paise),
        r.expires_at,
    )


class SqlWalletRepo:
    def __init__(self, conn: AsyncConnection, ids: IdGenerator, ledger: LedgerRepo) -> None:
        self._c = conn
        self._ids = ids
        self._ledger = ledger

    async def get(self, account_id: UUID) -> WalletRecord | None:
        q = sa.select(wallets).where(wallets.c.account_id == account_id)
        r = (await self._c.execute(q)).first()
        return _wallet(r) if r else None

    async def ensure(self, account_id: UUID) -> WalletRecord:
        """Create the wallet and its ledger account on the first top-up (idempotent)."""
        existing = await self.get(account_id)
        if existing is not None:
            return existing
        code = wallet_code(account_id)
        await self._c.execute(
            pg_insert(ledger_accounts)
            .values(id=self._ids.new(), code=code, kind="liability")
            .on_conflict_do_nothing(index_elements=["code"])
        )
        ledger_id = (
            await self._c.execute(
                sa.select(ledger_accounts.c.id).where(ledger_accounts.c.code == code)
            )
        ).scalar_one()
        await self._c.execute(
            pg_insert(wallets)
            .values(account_id=account_id, ledger_account_id=ledger_id)
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        found = await self.get(account_id)
        assert found is not None
        return found

    async def set_cap(self, account_id: UUID, cap: Paise | None) -> None:
        await self._c.execute(
            sa.update(wallets)
            .where(wallets.c.account_id == account_id)
            .values(monthly_cap_paise=cap)
        )

    async def balance(self, account_id: UUID, *, lock: bool = False) -> int:
        """Spendable value in taxable paise (the ledger balance of a liability, negated)."""
        code = wallet_code(account_id)
        value = await self._ledger.lock_balance(code) if lock else await self._ledger.balance(code)
        return -value

    async def add_lot(self, lot: CreditLot) -> None:
        await self._c.execute(
            sa.insert(credit_lots).values(
                id=lot.id,
                wallet_account_id=lot.wallet_account_id,
                payment_id=lot.payment_id,
                amount_paise=lot.amount_paise,
                remaining_paise=lot.remaining_paise,
                expires_at=lot.expires_at,
            )
        )

    async def lots(self, ledger_account_id: UUID) -> Sequence[CreditLot]:
        q = (
            sa.select(credit_lots)
            .where(
                credit_lots.c.wallet_account_id == ledger_account_id,
                credit_lots.c.remaining_paise > 0,
            )
            .order_by(credit_lots.c.expires_at, credit_lots.c.id)
        )
        return [_lot(r) for r in (await self._c.execute(q)).all()]

    async def debit(self, account_id: UUID, price_incl_gst: Paise, key: str, now: datetime) -> int:
        """Pay for usage from credits. Returns the taxable paise taken from the lots, which is
        less than asked only if the lots cannot cover it (then only that much is posted)."""
        wallet = await self.get(account_id)
        if wallet is None:
            return 0
        await self._ledger.lock_balance(wallet_code(account_id))  # serialises debits and expiry
        want = int(taxable_of(price_incl_gst))
        taken = 0
        rows = await self._c.execute(
            sa.select(credit_lots)
            .where(
                credit_lots.c.wallet_account_id == wallet.ledger_account_id,
                credit_lots.c.remaining_paise > 0,
                credit_lots.c.expires_at > now,
            )
            .order_by(credit_lots.c.expires_at, credit_lots.c.id)
            .with_for_update()
        )
        for lot in rows.all():
            if taken >= want:
                break
            use = min(int(lot.remaining_paise), want - taken)
            await self._c.execute(
                sa.update(credit_lots)
                .where(credit_lots.c.id == lot.id)
                .values(remaining_paise=credit_lots.c.remaining_paise - use)
            )
            taken += use
        if taken:
            await self._ledger.post(wallet_debit(Paise(taken), account_id, key), "usage", key, now)
        return taken

    async def expired_lots(self, now: datetime, limit: int) -> Sequence[tuple[UUID, CreditLot]]:
        """(billing account id, lot) of lots past their expiry that still have value."""
        q = (
            sa.select(credit_lots, wallets.c.account_id)
            .join(wallets, wallets.c.ledger_account_id == credit_lots.c.wallet_account_id)
            .where(credit_lots.c.remaining_paise > 0, credit_lots.c.expires_at <= now)
            .order_by(credit_lots.c.expires_at)
            .limit(limit)
        )
        return [(r.account_id, _lot(r)) for r in (await self._c.execute(q)).all()]

    async def expire(self, account_id: UUID, lot_id: UUID, now: datetime) -> int:
        """Move what is left of an expired lot to breakage revenue. Returns the paise."""
        await self._ledger.lock_balance(wallet_code(account_id))
        r = (
            await self._c.execute(
                sa.select(credit_lots).where(credit_lots.c.id == lot_id).with_for_update()
            )
        ).first()
        if r is None or r.remaining_paise <= 0 or r.expires_at > now:
            return 0
        left = int(r.remaining_paise)
        await self._c.execute(
            sa.update(credit_lots).where(credit_lots.c.id == lot_id).values(remaining_paise=0)
        )
        tx = credit_expiry(Paise(left), account_id, f"expire:{lot_id}")
        await self._ledger.post(tx, "credit_lot", str(lot_id), now)
        return left

    async def lots_expiring_between(
        self, start: datetime, end: datetime
    ) -> Sequence[tuple[UUID, CreditLot]]:
        q = (
            sa.select(credit_lots, wallets.c.account_id)
            .join(wallets, wallets.c.ledger_account_id == credit_lots.c.wallet_account_id)
            .where(
                credit_lots.c.remaining_paise > 0,
                credit_lots.c.expires_at >= start,
                credit_lots.c.expires_at < end,
            )
        )
        return [(r.account_id, _lot(r)) for r in (await self._c.execute(q)).all()]

    async def transactions(self, account_id: UUID, limit: int) -> list[dict[str, Any]]:
        """Newest first: top-ups, usage and expiries on the wallet's ledger account. `amount` is
        the change in spendable value (positive for a top-up, negative for spending)."""
        q = sa.text(
            "SELECT t.occurred_at, t.kind, t.ref_type, t.ref_id, -e.amount_paise AS amount_paise "
            "FROM billing.ledger_entries e "
            "JOIN billing.ledger_transactions t ON t.id = e.transaction_id "
            "JOIN billing.wallets w ON w.ledger_account_id = e.account_id "
            "WHERE w.account_id = :a ORDER BY e.id DESC LIMIT :n"
        )
        rows = await self._c.execute(q, {"a": account_id, "n": limit})
        return [dict(r._mapping) for r in rows.all()]

    async def all_balances(self) -> dict[UUID, int]:
        """Every wallet's spendable value by billing account (for the Valkey resync)."""
        q = sa.select(wallets.c.account_id, ledger_balances.c.balance_paise).join(
            ledger_balances,
            ledger_balances.c.account_id == wallets.c.ledger_account_id,
            isouter=True,
        )
        return {r.account_id: -int(r.balance_paise or 0) for r in (await self._c.execute(q)).all()}
