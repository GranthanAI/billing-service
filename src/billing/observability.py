import logging

import sentry_sdk
import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from billing.redaction import sentry_before_send, structlog_processor  # noqa: E402


def init_sentry(dsn: str, environment: str) -> bool:
    """Send errors to Sentry with personal data stripped. A no-op without a DSN."""
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        before_send=sentry_before_send,  # type: ignore[arg-type]
    )
    return True


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog_processor,
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

WEBHOOKS_RECEIVED = Counter(
    "billing_webhooks_received_total",
    "Webhook deliveries",
    ["event_type", "signature_ok"],
)
WEBHOOK_REJECTED = Counter("billing_webhook_rejected_total", "Webhooks with a bad signature")
WEBHOOK_FAILED = Counter(
    "billing_webhook_failed_total", "Inbox rows that ran out of attempts (alert in Phase 9)"
)
WEBHOOK_UNEXPECTED = Counter(
    "billing_webhook_unexpected_total",
    "Events that should never happen with our total counts (completed, expired)",
    ["event_type"],
)
REFUND_FAILED = Counter("billing_refund_failed_total", "Refunds Razorpay reported as failed")

USAGE_EVENTS = Counter(
    "billing_usage_events_total",
    "Usage events handled",
    ["kind", "result"],  # applied | duplicate | dlq
)
USAGE_COST = Counter(
    "billing_usage_cost_micro_inr_total",
    "Provider cost of rated usage, in micro-rupees",
    ["provider", "model_group", "plan"],
)
USAGE_LATE_COMMIT = Counter(
    "billing_usage_late_commit_total",
    "Usage that arrived after its hold had expired (or with no hold) and was counted then",
)

RECON_DIFFERENCE = Gauge(
    "billing_reconciliation_difference_paise",
    "Difference found by the last reconciliation, per check (units, not paise, for counters)",
    ["check"],
)

INBOX_BACKLOG = Gauge("billing_webhook_inbox_backlog", "Webhook inbox rows by status", ["status"])
PAYMENTS = Counter("billing_payments_total", "Payment attempts seen", ["method", "status"])
SUBSCRIPTIONS = Gauge(
    "billing_subscriptions", "Subscriptions by plan and status", ["plan", "status"]
)
DLQ_MESSAGES = Counter(
    "billing_dlq_messages_total", "Messages sent to a dead-letter topic", ["topic"]
)

AI_BUDGET = Gauge(
    "billing_daily_ai_budget_micro_inr", "DAILY_AI_BUDGET_INR in micro-rupees (alert threshold)"
)
