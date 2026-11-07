"""Wallet jobs: credits.expire (daily 01:00 IST), wallet.resync (every 5 minutes) and
wallet.mtd_reset (the first of the month, 00:00 IST).

Month-to-date spend is the `mtd` field of the wallet hash, zeroed by `wallet.mtd_reset` at the IST
month start (the alternative, a key per month, was not taken: the Lua script reads one hash)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from billing.app.events import CreditsExpiringEvent, StoragePaymentRequiredEvent
from billing.app.records import UsageEventRecord
from billing.domain.calendar import IST
from billing.domain.catalog import OwnerType
from billing.domain.entitlements import GrantSource
from billing.domain.money import MicroInr, Paise, display_paise, taxable_of
from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring
from billing.observability import STORAGE_BILLED

CREDITS_EXPIRE = "credits.expire"
WALLET_RESYNC = "wallet.resync"
WALLET_MTD_RESET = "wallet.mtd_reset"
STORAGE_BILL = "storage.bill"
SCHEDULES = (
    Recurring(CREDITS_EXPIRE, daily_at="01:00 IST"),
    Recurring(WALLET_RESYNC, every=timedelta(minutes=5)),
    Recurring(WALLET_MTD_RESET, monthly_on=1, at="00:00 IST"),
    Recurring(STORAGE_BILL, monthly_on=1, at="03:30 IST"),
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


# --- storage.bill ---------------------------------------------------------------------------

GIB = 1024**3
FREE_GIB = 10
STORAGE_CONSUMER = "storage-bill"


def over_gb_of(bytes_used: int) -> int:
    """Whole GiB above the free 10 GiB, rounded up (10 GiB exactly is 0; 10 GiB + 1 byte is 1)."""
    return max(0, -(-(bytes_used - FREE_GIB * GIB) // GIB))


def previous_ist_month(now: datetime) -> tuple[str, datetime]:
    """(YYYYMM, the instant the month ended) of the IST month before `now`."""
    first = now.astimezone(IST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = first - timedelta(days=1)
    return f"{last_day.year:04d}{last_day.month:02d}", first.astimezone(UTC)


@job_handler(STORAGE_BILL)
async def storage_bill(payload: dict[str, Any], ctx: JobContext) -> None:
    """On the 1st at 03:30 IST: charge Pay as you Go users for storage above 10 GiB in the IST month
    that just ended. Idempotent per user and month (`storage:{account}:{yyyymm}`). A wallet that
    cannot cover it is debited what it holds (never negative), the rest is recorded on the usage
    row and `storage.payment_required` tells file-service to block new uploads. One user failing
    is logged and counted; the batch goes on, and the job then fails once so it is retried (the
    users already billed are skipped by the dedupe)."""
    billing = ctx.billing
    if billing is None or ctx.storage_usage is None or ctx.entitlements is None:
        raise RuntimeError("storage.bill needs the billing context, StorageUsage and entitlements")
    if not billing.config.payg_enabled:
        log.info("storage_bill_skipped", reason="PAYG_ENABLED is off")
        return
    now = ctx.clock.now()
    month, occurred_at = previous_ist_month(now)
    if payload.get("month"):  # a manual rerun for an earlier month: YYYYMM
        month = str(payload["month"])
        year, mon = int(month[:4]), int(month[4:])
        occurred_at = datetime(year + mon // 12, mon % 12 + 1, 1, tzinfo=IST).astimezone(UTC)
    cfg = billing.config
    async with ctx.uow_factory() as uow:
        wallets = await uow.wallets.user_wallets()
    failed = 0
    for account_id, user_id in wallets:
        try:
            result = await _bill_one(ctx, account_id, user_id, month, occurred_at, now)
        except Exception:
            failed += 1
            result = "error"
            log.warning("storage_bill_failed", user_id=str(user_id), month=month, exc_info=True)
        STORAGE_BILLED.labels(result=result).inc()
    log.info(
        "storage_billed", month=month, wallets=len(wallets), failed=failed, gst=cfg.gst_registered
    )
    if failed:
        raise RuntimeError(f"storage.bill: {failed} users failed; retrying the batch")


async def _bill_one(
    ctx: JobContext,
    account_id: UUID,
    user_id: UUID,
    month: str,
    occurred_at: datetime,
    now: datetime,
) -> str:
    assert (
        ctx.billing is not None and ctx.entitlements is not None and ctx.storage_usage is not None
    )
    cfg = ctx.billing.config
    ent = await ctx.entitlements.resolve(user_id)
    if ent.grant.source is not GrantSource.PAYG:
        return "skipped_plan"  # a subscription plan: file-service enforces its storage limit
    used = await ctx.storage_usage.bytes_used(user_id)  # outside any transaction
    over_gb = over_gb_of(used)
    if over_gb == 0:
        return "free"
    price = Paise(over_gb * cfg.storage_gb_month_paise)  # GST-inclusive
    source_id = f"storage:{account_id}:{month}"
    async with ctx.uow_factory() as uow:
        if not await uow.processed.claim(STORAGE_CONSUMER, source_id):
            return "duplicate"
        want = int(taxable_of(price))
        taken = await uow.wallets.debit(account_id, price, source_id, now)
        shortfall = want - taken
        fx = await uow.pricing.fx_at(occurred_at)
        await uow.usage.insert(
            UsageEventRecord(
                kind="storage",
                source_id=source_id,
                occurred_at=occurred_at,
                user_id=user_id,
                billing_account_id=account_id,
                plan_version_id=ent.grant.plan_version.id,
                billable_units=over_gb,
                cost_micro_inr=MicroInr(0),
                fx_usd_inr=fx,
                price_paise=price,
                shortfall_taxable_paise=shortfall,
            )
        )
        if shortfall > 0:
            await uow.publisher.enqueue(
                StoragePaymentRequiredEvent(
                    user_id,
                    account_id,
                    over_gb,
                    int(display_paise(Paise(shortfall), cfg.gst_registered)),
                    month,
                )
            )
    if taken and ctx.counters is not None:
        await ctx.counters.wallet_credit(account_id, -taken)
    return "shortfall" if shortfall > 0 else "billed"
