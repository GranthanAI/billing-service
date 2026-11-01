import logging

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest


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
