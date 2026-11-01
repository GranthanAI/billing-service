"""Valkey key builders. `{uid}` is a hash tag: a move to Valkey Cluster keeps one user's keys in
one slot. Days and months are Asia/Kolkata calendar periods."""

from datetime import date, datetime
from uuid import UUID

from billing.domain.calendar import ist_day_key, ist_month_key

HOLDS_DUE = "holds:due"


def ent(uid: UUID) -> str:
    return f"ent:{{{uid}}}"


def day_counters(uid: UUID, now: datetime) -> str:
    return f"use:{{{uid}}}:d:{ist_day_key(now)}"


def day_counters_for(uid: UUID, day: date) -> str:
    return f"use:{{{uid}}}:d:{day:%Y%m%d}"


def month_counters(uid: UUID, now: datetime) -> str:
    return f"use:{{{uid}}}:m:{ist_month_key(now)}"


def month_counters_for(uid: UUID, year: int, month: int) -> str:
    return f"use:{{{uid}}}:m:{year:04d}{month:02d}"


def hold(uid: UUID, hold_id: str) -> str:
    return f"hold:{{{uid}}}:{hold_id}"


def due_member(uid: UUID, hold_id: str) -> str:
    return f"{uid}:{hold_id}"


def idem(uid: UUID, key: str) -> str:
    return f"idem:{{{uid}}}:{key}"


def done(uid: UUID, source_id: str) -> str:
    return f"done:{{{uid}}}:{source_id}"


def wallet(account: str) -> str:
    return f"wal:{{{account}}}"


def threshold(uid: UUID, limit: str, period: str, percent: int) -> str:
    return f"thr:{{{uid}}}:{limit}:{period}:{percent}"
