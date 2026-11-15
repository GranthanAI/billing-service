from datetime import timedelta
from typing import Any

import httpx
import structlog

from billing.jobs.runner import JobContext, job_handler
from billing.jobs.scheduler import Recurring
from billing.observability import MODELS_UNAVAILABLE

KIND = "models.availability"
# every 5 minutes; the first tick after a start enqueues the current slot at once, so it also runs at startup
SCHEDULE = Recurring(KIND, every=timedelta(minutes=5))
log = structlog.get_logger()
URL: dict[str, str] = {"base": "", "token": ""}  # set by main_worker from LLM_SERVICE_URL / LLM_SERVICE_TOKEN


async def fetch_availability(base_url: str, token: str, timeout_s: float = 5.0) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/internal/models/availability", headers={"X-Service-Token": token})
        resp.raise_for_status()
        return list(resp.json()["models"])


@job_handler(KIND)
async def refresh_availability(payload: dict[str, Any], ctx: JobContext) -> None:
    """Copy llm-service's availability into Valkey. llm-service unreachable: log and keep the old set (it expires by
    itself after 15 minutes, which is the fail-open path)."""
    if ctx.availability is None or not URL["base"]:
        return
    try:
        models = await fetch_availability(URL["base"], URL["token"])
    except Exception as exc:  # noqa: BLE001
        log.warning("models_availability_unreachable", error=type(exc).__name__)
        return
    down = await ctx.availability.store(models)
    MODELS_UNAVAILABLE.set(down)
    if down:
        log.warning("models_unavailable", count=down, models=[m["id"] for m in models if not m.get("available", True)])
