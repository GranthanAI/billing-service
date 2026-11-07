"""Stage G: the wallet ledger under any order of top-ups, spending, holds, expiries, resyncs and
storage bills, against real Postgres and Valkey.

A Hypothesis RuleBasedStateMachine: 25 examples in the ci profile (keeps `make test-all` short),
2,000 with HYPOTHESIS_PROFILE=nightly, which must pass before go-live). Every
example gets its own user, organisation, wallets and hold ids, so examples share one database
without seeing each other. After every step:

1. every ledger transaction touching these wallets balances;
2. -ledger balance == the sum of the lots' remaining_paise, per wallet;
3. the Valkey balance == the ledger balance plus what lot expiry has taken since the last resync
   (the one known in-flight amount), and a resync of a wallet with no open holds closes the gap;
4. `held` == the sum of the open holds' prices, and is >= 0;
5. no lot has remaining < 0 or > its amount;
6. the displayed (GST-inclusive) balance is within one paisa per top-up, debit and expiry of
   the GST-inclusive arithmetic (gross in minus unit prices out). The brief's "within 1 paisa for
   any sequence" cannot hold: every debit rounds on its own (see reports/stage-G.md).
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from billing.app.admin import Staff
from billing.app.org_topups import OrgTopups
from billing.app.records import OrgInfo
from billing.app.reserve import CheckAndReserveRequest
from billing.app.usage import UsageCall, UsageEvent
from billing.domain.catalog import ModelGroup, OrgVariant, OwnerType, ReserveKind
from billing.domain.money import Paise, display_paise, taxable_of
from billing.jobs.handlers.wallet import GIB, over_gb_of
from tests.integration.billing_env import START, STATE_BIHAR, Env
from tests.integration.test_wallet import payg_prices, services

PACKS = {"ind_199": 19900, "ind_499": 49900, "ind_999": 99900, "ind_1999": 199900}
MODELS = {
    ModelGroup.FAST: ("groq", "openai/gpt-oss-20b"),
    ModelGroup.SMART: ("anthropic", "claude-haiku-5-5"),
    ModelGroup.BEST: ("anthropic", "claude-sonnet-5-5"),
    ModelGroup.ULTRA: ("anthropic", "claude-opus-5-5"),
}
HOLD_SECONDS = 600
# usage_events is partitioned by month and only the next few months exist (production adds more
# with a job), so usage is stamped inside them however far the test clock has been moved, and
# storage.bill (which stamps its row from the clock) only runs while the clock is inside them.
PARTITIONED_UNTIL = START + timedelta(days=70)


@dataclass
class Hold:
    hold_id: str
    user: UUID
    group: ModelGroup
    wallet: UUID | None  # None: paid by the plan
    due: Any


@dataclass
class Book:
    """The GST-inclusive arithmetic for one wallet."""

    gross_in: int = 0
    gross_out: int = 0
    events: int = 0  # top-ups, debits and expiries: each rounds once, so each may move a paisa
    unsynced_expiry: int = 0  # taken from lots by expiry; the Valkey mirror has not heard yet


def make_machine(env: Env, loop: asyncio.AbstractEventLoop) -> type[RuleBasedStateMachine]:
    reserve, usage, wallet_service = services(env)
    unit = payg_prices().unit_prices
    topups = OrgTopups(env.bctx)

    class Machine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            env.clock.set(START)  # each example starts on the same day
            self.user = uuid4()
            self.org = OrgInfo(
                uuid4(), "Acme Pvt Ltd", OrgVariant.COMPANY, "10BBBBB1111B1Z5", "a@acme.test",
                STATE_BIHAR, None,
            )  # fmt: skip
            env.orgs.add_org(self.org)
            env.orgs.join(self.user, self.org.org_id, OrgVariant.COMPANY)
            self.run(self.start())
            self.n = 0
            self.open: list[Hold] = []
            self.expired: list[Hold] = []
            self.books: dict[UUID, Book] = {}
            self.invariant_runs = 0

        # --- plumbing ---------------------------------------------------------------------

        def run(self, coro: Any) -> Any:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=120)

        async def start(self) -> None:
            await env.admin.create_override(  # an org plan, so Ultra overflows onto the org wallet
                Staff(uuid4(), "billing_admin"), OwnerType.ORG, self.org.org_id, "org_pro",
                "property test", env.clock.now() + timedelta(days=30),
            )  # fmt: skip

        def book(self, account: UUID) -> Book:
            return self.books.setdefault(account, Book())

        async def accounts(self) -> dict[UUID, UUID]:
            """{billing account id: ledger account id} of this example's wallets."""
            rows = await env.sql(
                "SELECT w.account_id, w.ledger_account_id FROM billing.wallets w "
                "JOIN billing.billing_accounts a ON a.id = w.account_id "
                "WHERE a.owner_id IN (:u, :o)",
                u=self.user,
                o=self.org.org_id,
            )
            return {r.account_id: r.ledger_account_id for r in rows}

        async def ledger_balance(self, account: UUID) -> int:
            """The wallet's spendable value in taxable paise, from the ledger."""
            value = await env.scalar(
                "SELECT coalesce(-sum(e.amount_paise), 0) FROM billing.ledger_entries e "
                "JOIN billing.wallets w ON w.ledger_account_id = e.account_id "
                "WHERE w.account_id = :a",
                a=account,
            )
            return int(value)  # type: ignore[call-overload]

        async def personal(self) -> UUID | None:
            value = await env.scalar(
                "SELECT id FROM billing.billing_accounts WHERE owner_type = 'user' "
                "AND owner_id = :u",
                u=self.user,
            )
            return UUID(str(value)) if value else None

        async def observe_debit(self, account: UUID, before: int, price: int | None) -> None:
            """Account for a change in the ledger balance: a full unit price, or a partial one."""
            delta = before - await self.ledger_balance(account)
            if delta <= 0:
                return
            book = self.book(account)
            full = price is not None and delta == int(taxable_of(Paise(price)))
            book.gross_out += (
                price if full and price is not None else int(display_paise(Paise(delta)))
            )
            book.events += 1

        # --- rules ------------------------------------------------------------------------

        @rule(pack=st.sampled_from(sorted(PACKS)))
        def user_top_up(self, pack: str) -> None:
            async def go() -> None:
                options = await wallet_service.start_topup(
                    OwnerType.USER, self.user, pack_code=pack, email=None, state_code=STATE_BIHAR
                )
                hook, _, _ = env.gateway.pay_order(options.order_id)
                await env.deliver(hook)
                await env.process()
                account = await self.personal()
                assert account is not None
                book = self.book(account)
                book.gross_in += PACKS[pack]
                book.events += 1  # each top-up rounds its taxable value once

            self.run(go())

        @rule(extra_rupees=st.integers(0, 3000))
        def org_top_up(self, extra_rupees: int) -> None:
            amount = Paise(500_000 + 100 * extra_rupees)  # at least Rs 5,000, whole rupees

            async def go() -> None:
                made = await topups.create(self.org, amount, actor=uuid4(), reason="property")
                await topups.mark_paid(
                    self.org.org_id, made["request_id"], reference=f"UTR-{uuid4()}",
                    actor=uuid4(), reason="property",
                )  # fmt: skip
                account = UUID(
                    str(
                        await env.scalar(
                            "SELECT id FROM billing.billing_accounts WHERE owner_type = 'org' "
                            "AND owner_id = :o",
                            o=self.org.org_id,
                        )
                    )
                )
                book = self.book(account)
                book.gross_in += int(amount)
                book.events += 1  # each top-up rounds its taxable value once

            self.run(go())

        @rule(group=st.sampled_from(list(ModelGroup)))
        def reserve_a_message(self, group: ModelGroup) -> None:
            self.n += 1

            async def go() -> None:
                res = await reserve.check_and_reserve(
                    CheckAndReserveRequest(self.user, f"idem-{uuid4()}", ReserveKind.MESSAGE, group)
                )
                if not res.allowed:
                    return
                paid = res.funded_by == "credits"
                self.open.append(
                    Hold(
                        res.hold_id, self.user, group,
                        res.billing_account_id if paid else None,
                        env.clock.now() + timedelta(seconds=HOLD_SECONDS),
                    )
                )  # fmt: skip

            self.run(go())

        async def apply_usage(self, hold: Hold) -> None:
            self.n += 1
            provider, model = MODELS[hold.group]
            await usage.apply(
                UsageEvent(
                    ReserveKind.MESSAGE, f"req-{self.n}-{uuid4()}", f"evt-{self.n}-{uuid4()}",
                    hold.user, START, "success", hold.hold_id, hold.group.value, model,
                    (UsageCall(provider, model, "answer", 1000, 0, 200),),
                )
            )  # fmt: skip

        @rule(pick=st.integers(0, 10**6))
        def commit_a_hold(self, pick: int) -> None:
            if not self.open:
                return
            hold = self.open.pop(pick % len(self.open))

            async def go() -> None:
                before = await self.ledger_balance(hold.wallet) if hold.wallet else 0
                await self.apply_usage(hold)
                if hold.wallet:
                    await self.observe_debit(hold.wallet, before, int(unit[hold.group.value]))

            self.run(go())

        @rule(pick=st.integers(0, 10**6))
        def release_a_hold(self, pick: int) -> None:
            if not self.open:
                return
            hold = self.open.pop(pick % len(self.open))
            assert self.run(reserve.release(hold.user, hold.hold_id, "failed")) is True

        @rule()
        def let_holds_expire_and_sweep(self) -> None:
            env.clock.advance(seconds=HOLD_SECONDS + 1)
            self.run(env.run_job("holds.sweep"))
            now = env.clock.now()
            self.expired += [h for h in self.open if h.due <= now]
            self.open = [h for h in self.open if h.due > now]

        @rule(pick=st.integers(0, 10**6))
        def late_commit(self, pick: int) -> None:
            """The answer arrives after the sweeper gave the message back: counted, not charged."""
            if not self.expired:
                return
            hold = self.expired.pop(pick % len(self.expired))

            async def go() -> None:
                before = await self.ledger_balance(hold.wallet) if hold.wallet else 0
                await self.apply_usage(hold)
                if hold.wallet:
                    assert await self.ledger_balance(hold.wallet) == before

            self.run(go())

        @rule(pick=st.integers(0, 10**6))
        def lot_expiry(self, pick: int) -> None:
            async def go() -> None:
                lots = await env.sql(
                    "SELECT l.expires_at, w.account_id FROM billing.credit_lots l "
                    "JOIN billing.wallets w ON w.ledger_account_id = l.wallet_account_id "
                    "JOIN billing.billing_accounts a ON a.id = w.account_id "
                    "WHERE a.owner_id IN (:u, :o) AND l.remaining_paise > 0 ORDER BY l.id",
                    u=self.user,
                    o=self.org.org_id,
                )
                if not lots:
                    return
                lot = lots[pick % len(lots)]
                if lot.expires_at > env.clock.now():
                    env.clock.set(lot.expires_at + timedelta(seconds=1))  # past 12 months
                before = {a: await self.ledger_balance(a) for a in await self.accounts()}
                await env.run_job("credits.expire")
                for account, was in before.items():
                    taken = was - await self.ledger_balance(account)
                    if taken > 0:
                        book = self.book(account)
                        book.gross_out += int(display_paise(Paise(taken)))
                        book.events += 1
                        book.unsynced_expiry += taken

            self.run(go())

        @rule()
        def resync(self) -> None:
            async def go() -> None:
                await env.run_job("wallet.resync")
                for account in await self.accounts():
                    held = (await env.counters.wallet_get(account)).get("held", 0)
                    if held == 0:
                        self.book(account).unsynced_expiry = 0  # overwritten from the ledger

            self.run(go())

        @rule(used=st.integers(0, 40 * GIB))
        def storage_bill(self, used: int) -> None:
            async def go() -> None:
                if env.clock.now() > PARTITIONED_UNTIL:
                    return
                account = await self.personal()
                env.storage_usage.used[self.user] = used
                before = await self.ledger_balance(account) if account else 0
                await env.run_job("storage.bill")
                if account is None:
                    return
                price = over_gb_of(used) * 1000
                await self.observe_debit(account, before, price or None)

            self.run(go())

        # --- invariants -------------------------------------------------------------------

        @invariant()
        def the_wallet_ledger_holds(self) -> None:
            self.run(self.check())

        async def check(self) -> None:
            accounts = await self.accounts()
            if not accounts:
                return
            ledger_ids = list(accounts.values())
            async with env.app_engine.connect() as conn:
                unbalanced = (
                    await conn.execute(
                        sa.text(
                            "SELECT transaction_id FROM billing.ledger_entries "
                            "WHERE transaction_id IN ("
                            "SELECT transaction_id FROM billing.ledger_entries "
                            "WHERE account_id IN :ids) "
                            "GROUP BY 1 HAVING sum(amount_paise) <> 0"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": ledger_ids},
                    )
                ).all()
                bad_lots = (
                    await conn.execute(
                        sa.text(
                            "SELECT id FROM billing.credit_lots WHERE wallet_account_id IN :ids "
                            "AND (remaining_paise < 0 OR remaining_paise > amount_paise)"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": ledger_ids},
                    )
                ).all()
            assert unbalanced == []  # 1
            assert bad_lots == []  # 5
            for account, ledger_id in accounts.items():
                balance = await self.ledger_balance(account)
                lots = await env.scalar(
                    "SELECT coalesce(sum(remaining_paise), 0) FROM billing.credit_lots "
                    "WHERE wallet_account_id = :l",
                    l=ledger_id,
                )
                assert balance == int(lots) >= 0  # type: ignore[call-overload]  # 2
                book = self.book(account)
                mirror = await env.counters.wallet_get(account)
                assert mirror.get("balance", 0) == balance + book.unsynced_expiry  # 3
                open_prices = sum(
                    int(taxable_of(Paise(int(unit[h.group.value]))))
                    for h in self.open
                    if h.wallet == account
                )
                assert mirror.get("held", 0) == open_prices >= 0  # 4
                shown = int(display_paise(Paise(balance)))
                assert abs(shown - (book.gross_in - book.gross_out)) <= book.events  # 6

    return Machine


async def test_wallet_ledger_stays_balanced_and_in_step_with_valkey(env: Env) -> None:
    loop = asyncio.get_running_loop()
    case = make_machine(env, loop).TestCase
    nightly = os.environ.get("HYPOTHESIS_PROFILE") == "nightly"
    case.settings = settings(
        max_examples=2000 if nightly else 25,
        stateful_step_count=12,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    await asyncio.to_thread(case().runTest)
