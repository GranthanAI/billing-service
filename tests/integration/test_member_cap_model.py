"""Stateful model of the per-member cap in reserve.lua, commit.lua and release.lua: two members
share one organisation wallet. Whatever order reserve, commit, release, expire and late commit
arrive in, each member's `mtdm:` key equals the sum of their held and committed prices and never
exceeds the cap; the wallet's `held` equals the sum of the open holds; nothing goes negative."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from billing.adapters.valkey import keys
from billing.adapters.valkey.client import make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.app.ports import ReserveArgs
from billing.domain.catalog import DenyReason, ReserveKind
from billing.domain.money import Paise

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
CAP = 1000  # taxable paise per member and IST month
PRICES = [20, 50, 339, 1017]  # 1017 (Ultra) is above the cap, so it is always denied
FORTY_DAYS = 40 * 86400


def make_machine(valkey_url: str) -> type[RuleBasedStateMachine]:
    class Machine(RuleBasedStateMachine):
        holds = Bundle("holds")
        expired = Bundle("expired")

        def __init__(self) -> None:
            super().__init__()
            self.loop = asyncio.new_event_loop()
            self.client = make_valkey(valkey_url)
            self.counters = ValkeyCounters(self.client)
            self.wallet: UUID = uuid4()
            self.members = [uuid4(), uuid4()]
            self.balance = 100_000
            self.mtd = {m: 0 for m in self.members}  # held + committed prices
            self.open: dict[str, tuple[UUID, int]] = {}  # hold id -> (member, price)
            self.n = 0
            self.run(
                self.client.hset(
                    keys.wallet(str(self.wallet)),
                    mapping={"balance": self.balance, "held": 0, "mtd": 0, "cap": -1},
                )
            )

        def run(self, coro):  # type: ignore[no-untyped-def]
            return self.loop.run_until_complete(coro)

        def teardown(self) -> None:
            self.loop.run_until_complete(self.client.aclose())
            self.loop.close()

        def key(self, member: UUID) -> str:
            return keys.member_month(str(self.wallet), member, NOW)

        @rule(target=holds, member=st.integers(0, 1), price=st.sampled_from(PRICES))
        def reserve(self, member: int, price: int):  # type: ignore[no-untyped-def]
            m = self.members[member]
            args = ReserveArgs(
                uid=m, account=self.wallet, plan_version_id=uuid4(), idem_key=str(uuid4()),
                kind=ReserveKind.MESSAGE, period="day", now=NOW, mode="payg", field="best",
                limit=-1, best_limit=-1, price_paise=Paise(price), hold_id=str(uuid4()),
                hold_ttl=600, no_credit_fast=0, member_cap=CAP,
            )  # fmt: skip
            res = self.run(self.counters.reserve(args))
            held = sum(p for _, p in self.open.values())
            if self.mtd[m] + price > CAP:
                assert not res.allowed and res.deny_reason is DenyReason.MEMBER_SPEND_CAP
                return None
            if self.balance - held < price:
                assert not res.allowed and res.deny_reason is DenyReason.NO_CREDIT
                return None
            assert res.allowed
            self.mtd[m] += price
            self.open[args.hold_id] = (m, price)
            return (m, args.hold_id)

        @rule(hold=consumes(holds), charged=st.sampled_from([0, 1, 2]))
        def commit(self, hold: tuple[UUID, str] | None, charged: int) -> None:
            if hold is None:
                return
            m, hold_id = hold
            _, price = self.open.pop(hold_id)
            paid = [0, price // 2, price][charged]
            self.n += 1
            out = self.run(
                self.counters.commit(
                    m, hold_id, Paise(paid), f"src-{self.n}", ReserveKind.MESSAGE, "best", NOW
                )
            )
            assert out == "OK"
            self.balance -= paid
            self.mtd[m] += paid - price

        @rule(hold=consumes(holds))
        def release(self, hold: tuple[UUID, str] | None) -> None:
            if hold is None:
                return
            m, hold_id = hold
            _, price = self.open.pop(hold_id)
            assert self.run(self.counters.release(m, hold_id, "refund")) is True
            self.mtd[m] -= price

        @rule(target=expired, hold=consumes(holds))
        def expire(self, hold: tuple[UUID, str] | None):  # type: ignore[no-untyped-def]
            if hold is None:
                return None
            m, hold_id = hold
            _, price = self.open.pop(hold_id)
            assert self.run(self.counters.release(m, hold_id, "expire")) is True
            self.mtd[m] -= price
            return (m, hold_id)

        @rule(hold=consumes(expired))
        def late_commit(self, hold: tuple[UUID, str] | None) -> None:
            """The answer arrived after the hold expired: counted, and the member key is left
            alone. Nothing is charged: UsageService only charges credits when the hold exists."""
            if hold is None:
                return
            m, hold_id = hold
            self.n += 1
            out = self.run(
                self.counters.commit(
                    m, hold_id, Paise(0), f"late-{self.n}", ReserveKind.MESSAGE, "best", NOW
                )
            )
            assert out == "OK"

        @invariant()
        def member_keys_match_the_model(self) -> None:
            for m in self.members:
                raw = self.run(self.client.get(self.key(m)))
                assert int(raw or 0) == self.mtd[m]
                assert 0 <= self.mtd[m] <= CAP
                if raw is not None:
                    assert 0 < self.run(self.client.ttl(self.key(m))) <= FORTY_DAYS

        @invariant()
        def wallet_held_is_the_sum_of_open_holds(self) -> None:
            w = self.run(self.client.hgetall(keys.wallet(str(self.wallet))))
            assert int(w["held"]) == sum(p for _, p in self.open.values()) >= 0
            assert int(w["balance"]) == self.balance >= 0

    return Machine


def test_member_cap_matches_the_model(valkey_url: str) -> None:
    case = make_machine(valkey_url).TestCase
    examples = 2000 if os.environ.get("HYPOTHESIS_PROFILE") == "nightly" else 200
    case.settings = settings(max_examples=examples, stateful_step_count=30, deadline=None)
    case().runTest()
