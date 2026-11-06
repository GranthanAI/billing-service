"""The Counters port over Valkey. Only the Lua scripts change counters and holds; Python never
does read-modify-write on them."""

from datetime import UTC, date, datetime
from importlib import resources
from uuid import UUID

import valkey.asyncio as valkey_asyncio

from billing.adapters.valkey import keys
from billing.adapters.valkey.client import aw
from billing.app.ports import Hold, ReserveArgs, ReserveResult
from billing.domain.catalog import DenyReason, ModelGroup, ReserveKind
from billing.domain.money import Paise
from billing.observability import HOLDS_EXPIRED

SCRIPT_NAMES = ("reserve.lua", "commit.lua", "release.lua")


def load_script(name: str) -> str:
    return (resources.files("billing.adapters.valkey") / "scripts" / name).read_text("utf-8")


def parse_reserve(raw: str, kind: ReserveKind) -> ReserveResult:
    parts = raw.split("|")
    if parts[0] == "ALLOW":
        _, field, hold_id, price, downgraded, total = parts
        return ReserveResult(
            True, None, field, hold_id, Paise(int(price)), downgraded == "1", int(total)
        )
    reason = {
        "LIMIT": DenyReason.MONTHLY_LIMIT
        if kind is ReserveKind.YOUTUBE_VIDEO
        else DenyReason.DAILY_LIMIT,
        "NO_CREDIT": DenyReason.NO_CREDIT,
        "SPEND_CAP": DenyReason.SPEND_CAP,
    }[parts[1]]
    return ReserveResult(False, reason, None, None, Paise(0), False, 0)


def kind_of_field(field: str) -> ReserveKind:
    if field == "youtube":
        return ReserveKind.YOUTUBE_VIDEO
    if field == "manim":
        return ReserveKind.MANIM_VIDEO
    return ReserveKind.MESSAGE


class ValkeyCounters:
    def __init__(
        self,
        client: valkey_asyncio.Valkey,
        *,
        day_ttl: int = 172_800,
        month_ttl: int = 40 * 86_400,
    ) -> None:
        self._c = client
        self._day_ttl = day_ttl
        self._month_ttl = month_ttl
        self._sources = {n: load_script(n) for n in SCRIPT_NAMES}
        # register_script: EVALSHA, falling back to SCRIPT LOAD on NOSCRIPT
        self._reserve = client.register_script(self._sources["reserve.lua"])
        self._commit = client.register_script(self._sources["commit.lua"])
        self._release = client.register_script(self._sources["release.lua"])

    async def ensure_scripts_loaded(self) -> bool:
        """SCRIPT LOAD all three; True when the server now has every one of them."""
        shas = [await self._c.script_load(src) for src in self._sources.values()]
        return all(await self._c.script_exists(*shas))

    @staticmethod
    def _wallet(uid: UUID, account: str | None) -> str:
        return keys.wallet(account or str(uid))

    # --- reserve -------------------------------------------------------------------------

    async def reserve(self, a: ReserveArgs) -> ReserveResult:
        if a.period == "month":
            counters, ttl = keys.month_counters(a.uid, a.now), self._month_ttl
        else:
            counters, ttl = keys.day_counters(a.uid, a.now), self._day_ttl
        raw: str = await self._reserve(
            keys=[
                counters,
                keys.idem(a.uid, a.idem_key),
                keys.hold(a.uid, a.hold_id),
                self._wallet(a.uid, str(a.account) if a.account else None),
                keys.HOLDS_DUE,
            ],
            args=[
                a.mode,
                a.field,
                a.limit,
                a.best_limit,
                int(a.price_paise),
                a.hold_id,
                int(a.now.timestamp()),
                a.hold_ttl,
                ttl,
                a.no_credit_fast,
                str(a.uid),
                str(a.account) if a.account else "",
                str(a.plan_version_id),
            ],
        )
        return parse_reserve(raw, a.kind)

    # --- commit / release ----------------------------------------------------------------

    async def commit(
        self,
        user_id: UUID,
        hold_id: str,
        charged: Paise,
        source_id: str,
        kind: ReserveKind,
        field: str,
        occurred_at: datetime,
    ) -> str:
        hold_key = keys.hold(user_id, hold_id)
        acct: str | None = await aw(self._c.hget(hold_key, "acct"))
        if kind is ReserveKind.YOUTUBE_VIDEO:
            counters, ttl = keys.month_counters(user_id, occurred_at), self._month_ttl
        else:
            counters, ttl = keys.day_counters(user_id, occurred_at), self._day_ttl
        result: str = await self._commit(
            keys=[
                hold_key,
                self._wallet(user_id, acct),
                keys.HOLDS_DUE,
                keys.done(user_id, source_id),
                counters,
            ],
            args=[keys.due_member(user_id, hold_id), int(charged), field, ttl],
        )
        return result

    async def release(self, user_id: UUID, hold_id: str, reason: str) -> bool:
        hold_key = keys.hold(user_id, hold_id)
        counters, mode, acct = await aw(self._c.hmget(hold_key, ["counters", "mode", "acct"]))
        result: str = await self._release(
            keys=[
                hold_key,
                counters or keys.day_counters(user_id, datetime.now(UTC)),  # unused when GONE
                self._wallet(user_id, acct or None),
                keys.HOLDS_DUE,
            ],
            args=[keys.due_member(user_id, hold_id), reason],
        )
        released = result == "OK"
        if released and reason == "expire":
            HOLDS_EXPIRED.labels(mode=mode or "plan").inc()
        return released

    # --- reads ---------------------------------------------------------------------------

    async def peek_hold(self, user_id: UUID, hold_id: str) -> Hold | None:
        data: dict[str, str] = await aw(self._c.hgetall(keys.hold(user_id, hold_id)))
        if not data:
            return None
        field = data["field"]
        score = await self._c.zscore(keys.HOLDS_DUE, keys.due_member(user_id, hold_id))
        counters = data["counters"]
        group = ModelGroup(field) if field in {g.value for g in ModelGroup} else None
        return Hold(
            hold_id=hold_id,
            user_id=user_id,
            kind=kind_of_field(field),
            group=group,
            mode=data["mode"],
            price=Paise(int(data["price"])),
            day_key=counters.rsplit(":", 1)[-1],
            billing_account_id=UUID(data["acct"]) if data.get("acct") else None,
            plan_version_id=UUID(data["pv"]),
            expires_at=datetime.fromtimestamp(score, UTC) if score is not None else None,
        )

    async def day_counts(self, user_id: UUID, day: date) -> dict[str, int]:
        data: dict[str, str] = await aw(self._c.hgetall(keys.day_counters_for(user_id, day)))
        return {k: int(v) for k, v in data.items()}

    async def erase_user(self, user_id: UUID) -> int:
        removed = 0
        async for key in self._c.scan_iter(match=f"*{{{user_id}}}*", count=500):
            removed += int(await self._c.delete(key))
        due = [
            m for m in await self._c.zrange(keys.HOLDS_DUE, 0, -1) if m.startswith(f"{user_id}:")
        ]
        if due:
            await self._c.zrem(keys.HOLDS_DUE, *due)
        return removed

    async def day_counts_all(self, day: date) -> dict[UUID, dict[str, int]]:
        out: dict[UUID, dict[str, int]] = {}
        async for key in self._c.scan_iter(match=f"use:{{*}}:d:{day:%Y%m%d}", count=500):
            uid = UUID(key.split("{", 1)[1].split("}", 1)[0])
            data: dict[str, str] = await aw(self._c.hgetall(key))
            out[uid] = {k: int(v) for k, v in data.items()}
        return out

    async def month_counts(self, user_id: UUID, now: datetime) -> dict[str, int]:
        data: dict[str, str] = await aw(self._c.hgetall(keys.month_counters(user_id, now)))
        return {k: int(v) for k, v in data.items()}

    async def set_once(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self._c.set(key, "1", nx=True, ex=max(1, ttl_seconds)))

    async def due_holds(self, now_unix: int, limit: int) -> list[tuple[UUID, str]]:
        members: list[str] = await self._c.zrangebyscore(
            keys.HOLDS_DUE, "-inf", now_unix, start=0, num=limit
        )
        out = []
        for m in members:
            uid, _, hold_id = m.partition(":")
            out.append((UUID(uid), hold_id))
        return out

    async def open_holds(self) -> int:
        return int(await self._c.zcard(keys.HOLDS_DUE))

    # --- rebuild (after Valkey loss) -----------------------------------------------------

    async def rebuild_day(self, user_id: UUID, day: date, counts: dict[str, int]) -> None:
        key = keys.day_counters_for(user_id, day)
        await self._write(key, counts, self._day_ttl)

    async def rebuild_month(
        self, user_id: UUID, year: int, month: int, counts: dict[str, int]
    ) -> None:
        await self._write(keys.month_counters_for(user_id, year, month), counts, self._month_ttl)

    async def _write(self, key: str, counts: dict[str, int], ttl: int) -> None:
        mapping = {**counts, "total": sum(counts.values())}
        async with self._c.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, ttl)
            await pipe.execute()
