from datetime import datetime, timedelta


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed")
    return dt


class TestClock:
    __test__ = False  # not a pytest test class

    def __init__(self, start: datetime) -> None:
        self._now = _aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = _aware(dt)
