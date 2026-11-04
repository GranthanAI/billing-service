"""Kafka topic names. The only place they are spelled out."""

# consumed
CHAT_RESPONSE_GENERATED = "chat.response.generated"
CHAT_RESPONSE_CANCELLED = "chat.response.cancelled"
ARTIFACT_MANIM_RENDERED = "artifact.manim.rendered"
FILE_YOUTUBE_INGESTED = "file.youtube.ingested"
USER_DELETED = "user.deleted"

# produced
SUBSCRIPTION_CHANGED = "subscription.changed"
PAYMENT_FAILED = "payment.failed"
USAGE_THRESHOLD_REACHED = "usage.threshold_reached"
INVOICE_ISSUED = "invoice.issued"

# dead letters
BILLING_USAGE_DLQ = "billing.usage.dlq"
BILLING_USER_DLQ = "billing.user.dlq"

CONSUMED = (
    CHAT_RESPONSE_GENERATED,
    CHAT_RESPONSE_CANCELLED,
    ARTIFACT_MANIM_RENDERED,
    FILE_YOUTUBE_INGESTED,
    USER_DELETED,
)
PRODUCED = (SUBSCRIPTION_CHANGED, PAYMENT_FAILED, USAGE_THRESHOLD_REACHED, INVOICE_ISSUED)
DLQS = (BILLING_USAGE_DLQ, BILLING_USER_DLQ)
ALL = (*CONSUMED, *PRODUCED, *DLQS)

if __name__ == "__main__":  # `python -m billing.app.topics` lists them for scripts
    import sys

    sys.stdout.buffer.write(("\n".join(ALL) + "\n").encode())  # LF even on Windows
