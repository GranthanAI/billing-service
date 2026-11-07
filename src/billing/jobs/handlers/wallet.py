"""Wallet jobs: credits.expire (daily 01:00 IST), wallet.resync (every 5 minutes) and
wallet.mtd_reset (the first of the month, 00:00 IST).

Month-to-date spend is the `mtd` field of the wallet hash, zeroed by `wallet.mtd_reset` at the IST
month start (the alternative, a key per month, was not taken: the Lua script reads one hash)."""

from datetime import timedelta
from typing import Any

import structlog

from billing.app.events import CreditsExpiringEvent
from billing.domain.catalog import OwnerType
from billing.domain.money import display_paise
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring

CREDITS_EXPIRE = "credits.expire"
WALLET_RESYNC = "wallet.resync"
WALLET_MTD_RESET = "wallet.mtd_reset"
SCHEDULES = (
    Recurring(CREDITS_EXPIRE, daily_at="01:00 IST"),
    Recurring(WALLET_RESYNC, every=timedelta(minutes=5)),
    Recurring(WALLET_MTD_RESET, monthly_on=1, at="00:00 IST"),
)
REMINDER_DAYS = 30
BATCH = 200
REMINDER_CONSUMER = "wallet-expiry-reminder"
log = structlog.get_logger()


@job_handler(CREDITS_EXPIRE)
async def credits_expire(payload: dict[str, Any], ctx: JobContext) -> None:
    """Breakage for lots past their expiry, and one reminder per lot about 30 days ahead."""
    now = ctx.clock.now()
    expired = 0
    while True:
        async with ctx.uow_factory() as uow:
            batch = list(await uow.wallets.expired_lots(now, BATCH))
        if not batch:
            break
        for account_id, lot in batch:
            async with ctx.uow_factory() as uow:
                if await uow.wallets.expire(account_id, lot.id, now):
                    expired += 1
    reminded = 0
    registered = ctx.billing.config.gst_registered if ctx.billing else True
    window_end = now + timedelta(days=REMINDER_DAYS)
    async with ctx.uow_factory() as uow:
        soon = list(await uow.wallets.lots_expiring_between(now, window_end))
    for account_id, lot in soon:
        async with ctx.uow_factory() as uow:
            account = await uow.accounts.get(account_id)
            if account is None or account.owner_type is not OwnerType.USER:
                continue  # an organisation's wallet is handled by staff
            if not await uow.processed.claim(REMINDER_CONSUMER, str(lot.id)):
                continue
            await uow.publisher.enqueue(
                CreditsExpiringEvent(
                    account.owner_id,
                    account.id,
                    lot.id,
                    int(display_paise(lot.remaining_paise, registered)),
                    int(lot.remaining_paise),
                    lot.expires_at,
                )
            )
            reminded += 1
    if expired or reminded:
        log.info("credits_expire", lots_expired=expired, reminders=reminded)


@job_handler(WALLET_RESYNC)
async def wallet_resync(payload: dict[str, Any], ctx: JobContext) -> None:
    """Overwrite `wal:*` from the ledger, skipping wallets with open holds."""
    if ctx.counters is None:
        raise RuntimeError("wallet.resync needs counters")
    async with ctx.uow_factory() as uow:
        balances = await uow.wallets.all_balances()
    repaired = 0
    for account_id, balance in balances.items():
        if await ctx.counters.wallet_sync(account_id, balance):
            repaired += 1
    log.info("wallet_resync", wallets=len(balances), synced=repaired)


@job_handler(WALLET_MTD_RESET)
async def wallet_mtd_reset(payload: dict[str, Any], ctx: JobContext) -> None:
    if ctx.counters is None:
        raise RuntimeError("wallet.mtd_reset needs counters")
    async with ctx.uow_factory() as uow:
        balances = await uow.wallets.all_balances()
    for account_id in balances:
        await ctx.counters.wallet_reset_mtd(account_id)
