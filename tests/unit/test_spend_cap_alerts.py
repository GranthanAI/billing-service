"""B4c: the monthly spend cap raises usage.threshold_reached at 80% and 100%, once each per month, plus an email."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from billing.app.events import UsageThresholdReachedEvent
from billing.app.usage import Recorded, UsageService
from billing.domain.catalog import ReserveKind

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
ACCOUNT = uuid4()


class FakeUow:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.jobs_enqueued: list[tuple[str, dict, str]] = []
        outer = self
        self.publisher = SimpleNamespace(enqueue=self._publish)
        self.accounts = SimpleNamespace(get=self._account)
        self.jobs = SimpleNamespace(enqueue=self._job)
        del outer

    async def _publish(self, event: object) -> None:
        self.events.append(event)

    async def _account(self, _id: object) -> object:
        return SimpleNamespace(id=ACCOUNT, billing_email="ada@example.com")

    async def _job(self, name: str, payload: dict, now: object, dedupe: str, **_: object) -> bool:
        self.jobs_enqueued.append((name, payload, dedupe))
        return True

    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeCounters:
    def __init__(self, cap: int | None, mtd: int = 0) -> None:
        self.wallet: dict[str, int] = {"mtd": mtd}
        if cap is not None:
            self.wallet["cap"] = cap
        self.once: set[str] = set()

    async def wallet_get(self, _account: object) -> dict[str, int]:
        return dict(self.wallet)

    async def set_once(self, key: str, _ttl: int) -> bool:
        if key in self.once:
            return False
        self.once.add(key)
        return True

    async def day_counts(self, *_: object) -> dict[str, int]:
        return {}


def build(cap: int | None, mtd: int = 0, email: bool = True):  # type: ignore[no-untyped-def]
    uow = FakeUow()
    counters = FakeCounters(cap, mtd)
    limits = SimpleNamespace(messages_per_day=-1, best_per_day=-1, youtube_per_month=-1)

    async def resolve(_user: object) -> object:
        return SimpleNamespace(limits=limits)

    svc = UsageService(
        lambda: uow,  # type: ignore[arg-type,return-value]
        SimpleNamespace(resolve=resolve),  # type: ignore[arg-type]
        counters,  # type: ignore[arg-type]
        SimpleNamespace(now=lambda: NOW),  # type: ignore[arg-type]
        config=SimpleNamespace(email_enabled=True) if email else None,  # type: ignore[arg-type]
    )
    return svc, uow, counters


EV = SimpleNamespace(user_id=uuid4(), kind=ReserveKind.MANIM_VIDEO, occurred_at=NOW)


def rec(charged: int = 100) -> Recorded:
    return Recorded(ACCOUNT, "payg", {}, True, charged=charged)


async def test_80_and_100_percent_fire_once_each_with_paise_and_the_first_of_next_month() -> None:
    svc, uow, counters = build(cap=10_000, mtd=8_000)
    await svc._thresholds(EV, rec())  # type: ignore[arg-type]
    await svc._thresholds(EV, rec())  # a second debit at the same level: nothing new
    counters.wallet["mtd"] = 10_000
    await svc._thresholds(EV, rec())
    await svc._thresholds(EV, rec())
    events = [e for e in uow.events if isinstance(e, UsageThresholdReachedEvent)]
    assert [(e.limit, e.percent, e.used, e.allowed) for e in events] == [
        ("spend_cap", 80, 8_000, 10_000),
        ("spend_cap", 100, 10_000, 10_000),
    ]
    assert events[0].resets_at.isoformat() == "2026-12-01T00:00:00+05:30"
    assert [p["template"] for _, p, _ in uow.jobs_enqueued] == [
        "spend_cap_alert",
        "spend_cap_alert",
    ]
    assert uow.jobs_enqueued[0][1]["data"] == {
        "percent": 80, "used_paise": 8_000, "cap_paise": 10_000, "resets_at": "2026-12-01T00:00:00+05:30",
    }  # fmt: skip
    assert len({d for _, _, d in uow.jobs_enqueued}) == 2  # each alert has its own dedupe key


async def test_below_80_percent_nothing_fires() -> None:
    svc, uow, _ = build(cap=10_000, mtd=7_999)
    await svc._thresholds(EV, rec())  # type: ignore[arg-type]
    assert uow.events == [] and uow.jobs_enqueued == []


async def test_no_cap_no_alert_and_no_credits_spent_no_alert() -> None:
    svc, uow, _ = build(cap=None, mtd=9_999)
    await svc._thresholds(EV, rec())  # type: ignore[arg-type]
    svc2, uow2, _ = build(cap=-1, mtd=9_999)  # -1 = no cap
    await svc2._thresholds(EV, rec())  # type: ignore[arg-type]
    svc3, uow3, _ = build(cap=10_000, mtd=10_000)
    await svc3._thresholds(EV, rec(charged=0))  # a plan-funded message does not spend the cap
    assert uow.events == uow2.events == uow3.events == []
    assert uow.jobs_enqueued == uow2.jobs_enqueued == uow3.jobs_enqueued == []


async def test_without_a_config_the_event_still_fires_but_no_email() -> None:
    svc, uow, _ = build(cap=10_000, mtd=10_000, email=False)
    await svc._thresholds(EV, rec())  # type: ignore[arg-type]
    assert len(uow.events) == 2 and uow.jobs_enqueued == []  # 80% and 100% both crossed at once
