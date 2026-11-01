"""UUIDv7 (RFC 9562 section 5.7): 48-bit Unix ms, 12-bit monotonic counter, random tail."""

import os
import threading
import time
from collections.abc import Callable
from uuid import UUID


class Uuid7Generator:
    """Monotonic within a process: ids from one generator always sort in creation order."""

    def __init__(
        self,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._now_ms = now_ms
        self._random = random_bytes
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def new(self) -> UUID:
        with self._lock:
            ms = self._now_ms()
            if ms > self._last_ms:
                self._last_ms = ms
                self._counter = int.from_bytes(self._random(2), "big") & 0x07FF  # keep headroom
            else:
                self._counter += 1
                if self._counter > 0x0FFF:  # counter overflow: borrow from the clock
                    self._last_ms += 1
                    self._counter = 0
                ms = self._last_ms
            counter = self._counter
        rand = int.from_bytes(self._random(8), "big") & ((1 << 62) - 1)
        value = (ms & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76  # version 7
        value |= counter << 64  # rand_a: 12-bit counter
        value |= 0b10 << 62  # RFC 4122 variant
        value |= rand  # rand_b: 62 random bits
        return UUID(int=value)


class SequentialIds:
    """Deterministic ids for tests: 00000000-...-000001, then 2, 3, ..."""

    def __init__(self, start: int = 1) -> None:
        self._next = start

    def new(self) -> UUID:
        value = UUID(int=self._next)
        self._next += 1
        return value
