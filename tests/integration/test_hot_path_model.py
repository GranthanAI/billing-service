"""Stateful model of the Lua scripts: whatever order reserve, commit, release, sweep and late
commit arrive in, the counters match a simple Python model and never go negative."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from billing.adapters.valkey import keys
from billing.adapters.valkey.client import make_valkey
from billing.adapters.valkey.counters import ValkeyCounters
from billing.app.ports import ReserveArgs
from billing.domain.catalog import ReserveKind
from billing.domain.money import Paise

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
LIMIT = 5


class ScriptHarness:
    """Owns an event loop and a client, so a synchronous state machine can drive the scripts."""

    def __init__(self, url: str) -> None:
        self.loop = asyncio.new_event_loop()
        self.client = make_valkey(url)
        self.counters = ValkeyCounters(self.client)
        self.uid: UUID = uuid4()
        self.account: UUID = uuid4()

    def run(self, coro):  # type: ignore[no-untyped-def]
        return self.loop.run_until_complete(coro)

    def close(self) -> None:
        self.loop.run_until_complete(self.client.aclose())
        self.loop.close()

    def args(self, mode: str, field: str, limit: int, price: int = 0) -> ReserveArgs:
        return ReserveArgs(
            uid=self.uid,
            account=self.account,
            plan_version_id=uuid4(),
            idem_key=str(uuid4()),
            kind=ReserveKind.MESSAGE,
            period="day",
            now=NOW,
            mode=mode,
            field=field,
            limit=limit,
            best_limit=-1,
            price_paise=Paise(price),
            hold_id=str(uuid4()),
            hold_ttl=600,
            no_credit_fast=0,
        )


def make_machine(valkey_url: str, mode: str) -> type[RuleBasedStateMachine]:
    class Machine(RuleBasedStateMachine):
        holds = Bundle("holds")
        expired = Bundle("expired")

        def __init__(self) -> None:
            super().__init__()
            self.h = ScriptHarness(valkey_url)
            self.total = 0  # what the day counter should say
            self.late = 0
            self.open: dict[str, int] = {}  # hold_id -> price (payg)
            self.balance = 100_000
            if mode == "payg":
                self.h.run(
                    self.h.client.hset(
                        keys.wallet(str(self.h.account)),
                        mapping={"balance": self.balance, "held": 0, "mtd": 0, "cap": -1},
                    )
                )
            self.day_key = keys.day_counters(self.h.uid, NOW)
            self.n = 0

        def teardown(self) -> None:
            self.h.close()

        @rule(target=holds, price=st.sampled_from([20, 50, 400]))
        def reserve(self, price: int):  # type: ignore[no-untyped-def]
            if mode == "plan":
                a = self.h.args("plan", "fast", LIMIT)
            else:
                a = self.h.args("payg", "smart", -1, price)
            res = self.h.run(self.h.counters.reserve(a))
            if mode == "plan":
                should_allow = self.total < LIMIT
            else:
                held = sum(self.open.values())
                should_allow = self.balance - held >= price
            assert res.allowed == should_allow
            if not res.allowed:
                return None
            self.total += 1
            if mode == "payg":
                self.open[a.hold_id] = price
            return a.hold_id

        @rule(hold=consumes(holds))
        def commit(self, hold: str | None) -> None:
            if hold is None:
                return
            self.n += 1
            price = self.open.pop(hold, 0)
            out = self.h.run(
                self.h.counters.commit(
                    self.h.uid,
                    hold,
                    Paise(price),
                    f"src-{self.n}",
                    ReserveKind.MESSAGE,
                    "fast" if mode == "plan" else "smart",
                    NOW,
                )
            )
            assert out == "OK"
            self.balance -= price

        @rule(hold=consumes(holds))
        def release_refund(self, hold: str | None) -> None:
            if hold is None:
                return
            assert self.h.run(self.h.counters.release(self.h.uid, hold, "refund")) is True
            self.total -= 1
            self.open.pop(hold, None)

        @rule(target=expired, hold=consumes(holds))
        def sweep_expire(self, hold: str | None):  # type: ignore[no-untyped-def]
            if hold is None:
                return None
            assert self.h.run(self.h.counters.release(self.h.uid, hold, "expire")) is True
            self.total -= 1
            self.open.pop(hold, None)
            return hold

        @rule(hold=consumes(expired))
        def late_commit(self, hold: str | None) -> None:
            if hold is None:
                return
            self.n += 1
            field = "fast" if mode == "plan" else "smart"
            args = (self.h.uid, hold, Paise(0), f"late-{self.n}", ReserveKind.MESSAGE, field, NOW)
            assert self.h.run(self.h.counters.commit(*args)) == "OK"  # counted again
            assert self.h.run(self.h.counters.commit(*args)) == "DUP"
            self.total += 1
            self.late += 1

        @invariant()
        def counters_match_the_model(self) -> None:
            data = self.h.run(self.h.client.hgetall(self.day_key))
            actual = int(data.get("total", 0))
            assert actual == self.total
            assert all(int(v) >= 0 for v in data.values())
            if mode == "plan":
                assert actual <= LIMIT + self.late  # only late commits may exceed the limit

        @invariant()
        def wallet_held_is_the_sum_of_open_holds(self) -> None:
            if mode != "payg":
                return
            w = self.h.run(self.h.client.hgetall(keys.wallet(str(self.h.account))))
            held = int(w["held"])
            assert held >= 0
            assert held == sum(self.open.values())
            assert int(w["balance"]) == self.balance

    return Machine


def test_plan_mode_counters_match_the_model(valkey_url: str) -> None:
    case = make_machine(valkey_url, "plan").TestCase
    case.settings = settings(max_examples=40, stateful_step_count=25, deadline=None)
    case().runTest()


def test_payg_mode_wallet_never_goes_negative(valkey_url: str) -> None:
    case = make_machine(valkey_url, "payg").TestCase
    case.settings = settings(max_examples=40, stateful_step_count=25, deadline=None)
    case().runTest()
