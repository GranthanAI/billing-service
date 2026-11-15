"""Which catalog models llm-service can answer right now (M2).

llm-service lists every registry model as available or not (no key, disabled, no adapter, breaker open). The
`models.availability` job copies the unavailable ones into Valkey (TTL 15 minutes). Everything here FAILS OPEN: with no data
(job never ran, llm-service down for 15 minutes) every model counts as available, so a monitoring gap never takes
models away from users."""

import json
import time
from typing import Any

KEY = "billing:models:unavailable"
TTL_SECONDS = 900
LOCAL_SECONDS = 10.0  # in-process copy: the reserve path reads this on every message


class ModelAvailability:
    def __init__(self, client: Any, *, monotonic: Any = time.monotonic) -> None:
        self._c = client
        self._mono = monotonic
        self._local: tuple[float, dict[str, str] | None] | None = None

    async def store(self, models: list[dict[str, Any]]) -> int:
        """Replace the set from llm-service's answer. Returns how many models are unavailable."""
        down = {m["id"]: str(m.get("reason") or "unavailable") for m in models if not m.get("available", True)}
        await self._c.set(KEY, json.dumps(down), ex=TTL_SECONDS)
        self._local = None
        return len(down)

    async def unavailable(self) -> dict[str, str] | None:
        """{model id: reason}; None when there is no data (fail open)."""
        now = self._mono()
        if self._local is not None and now - self._local[0] < LOCAL_SECONDS:
            return self._local[1]
        try:
            raw = await self._c.get(KEY)
        except Exception:  # noqa: BLE001  Valkey trouble must not stop a message
            return None
        data = None if raw is None else dict(json.loads(raw))
        self._local = (now, data)
        return data

    async def is_available(self, model: str) -> bool:
        down = await self.unavailable()
        return down is None or model not in down
