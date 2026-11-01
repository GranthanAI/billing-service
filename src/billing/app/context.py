"""Per-request / per-message context shared across layers."""

from contextvars import ContextVar

# Set by the API middleware and by Kafka consumers; read by the event codec when it fills the
# envelope, so one user action can be followed across services.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
