"""Kafka topic names. The only place they are spelled out."""

# consumed
CHAT_RESPONSE_GENERATED = "chat.response.generated"
CHAT_RESPONSE_CANCELLED = "chat.response.cancelled"
ARTIFACT_MANIM_RENDERED = "artifact.manim.rendered"
FILE_YOUTUBE_INGESTED = "file.youtube.ingested"
USER_DELETED = "user.deleted"
ORG_MEMBER_JOINED = "org.member.joined"
ORG_MEMBER_LEFT = "org.member.left"

# produced
SUBSCRIPTION_CHANGED = "subscription.changed"
PAYMENT_FAILED = "payment.failed"
SUBSCRIPTION_RENEWAL_UPCOMING = "subscription.renewal_upcoming"
PERSONAL_PLAN_REDUNDANT = "entitlement.personal_plan_redundant"
USAGE_THRESHOLD_REACHED = "usage.threshold_reached"
WALLET_CREDITS_EXPIRING = "wallet.credits_expiring"
STORAGE_PAYMENT_REQUIRED = "storage.payment_required"
INVOICE_ISSUED = "invoice.issued"

# dead letters
BILLING_USAGE_DLQ = "billing.usage.dlq"
BILLING_USER_DLQ = "billing.user.dlq"
BILLING_ORG_DLQ = "billing.org.dlq"

CONSUMED = (
    CHAT_RESPONSE_GENERATED,
    CHAT_RESPONSE_CANCELLED,
    ARTIFACT_MANIM_RENDERED,
    FILE_YOUTUBE_INGESTED,
    USER_DELETED,
    ORG_MEMBER_JOINED,
    ORG_MEMBER_LEFT,
)
PRODUCED = (
    SUBSCRIPTION_CHANGED,
    PAYMENT_FAILED,
    SUBSCRIPTION_RENEWAL_UPCOMING,
    PERSONAL_PLAN_REDUNDANT,
    USAGE_THRESHOLD_REACHED,
    WALLET_CREDITS_EXPIRING,
    STORAGE_PAYMENT_REQUIRED,
    INVOICE_ISSUED,
)
DLQS = (BILLING_USAGE_DLQ, BILLING_USER_DLQ, BILLING_ORG_DLQ)
ALL = (*CONSUMED, *PRODUCED, *DLQS)

if __name__ == "__main__":  # `python -m billing.app.topics` lists them for scripts
    import sys

    sys.stdout.buffer.write(("\n".join(ALL) + "\n").encode())  # LF even on Windows
