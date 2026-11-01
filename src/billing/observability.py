import logging

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


OUTBOX_OLDEST_UNPUBLISHED = Gauge(
    "billing_outbox_oldest_unpublished_seconds",
    "Age of the oldest outbox row that has not been published yet",
)
JOBS_DEAD = Counter("billing_jobs_dead_total", "Jobs that ran out of attempts", ["kind"])

RESERVE_REQUESTS = Counter(
    "billing_reserve_requests_total",
    "CheckAndReserve calls",
    ["kind", "decision", "reason", "plan"],
)
RESERVE_DURATION = Histogram(
    "billing_reserve_duration_seconds",
    "CheckAndReserve latency inside billing-service",
    ["kind"],
    buckets=(0.001, 0.002, 0.005, 0.010, 0.025, 0.050, 0.100),
)
RESERVE_DOWNGRADED = Counter(
    "billing_reserve_downgraded_total", "Best requests served as Smart", ["plan"]
)
HOLDS_OPEN = Gauge("billing_holds_open", "Open holds (ZCARD holds:due)")
HOLDS_EXPIRED = Counter("billing_holds_expired_total", "Holds the sweeper expired", ["mode"])
RELEASE_TOTAL = Counter("billing_release_total", "Release calls that gave a unit back", ["reason"])
LOOP_LAG = Histogram(
    "billing_event_loop_lag_seconds",
    "How late a 50 ms timer fires: a stalled event loop shows up here before latency does",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
