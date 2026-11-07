"""organisations: min_seats, invoice-mode subscriptions, and what an org invoice needs to carry

* plan_versions.min_seats: minimum seats of an organisation plan (null for individual plans)
* subscriptions.billing_mode: 'mandate' (Razorpay autopay) or 'invoice' (paid by link or transfer)
* subscriptions.pending_seats: a seat decrease that takes effect at the next renewal
* invoices.subscription_id: which subscription an invoice bills (null for card payments, which are
  found through the payment); seat_change_to: the seat count a true-up invoice raises the
  subscription to once paid; payment_link_id / payment_link_url: the Razorpay payment link
"""

from alembic import op

revision = "0015_orgs"
down_revision = "0015a_fee_excludes_gst"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE plan_versions ADD COLUMN min_seats int CHECK (min_seats IS NULL OR min_seats >= 1)",
    "ALTER TABLE subscriptions ADD COLUMN billing_mode text NOT NULL DEFAULT 'mandate' "
    "CHECK (billing_mode IN ('mandate','invoice'))",
    "ALTER TABLE subscriptions ADD COLUMN pending_seats int CHECK (pending_seats IS NULL OR pending_seats >= 1)",
    "ALTER TABLE invoices ADD COLUMN subscription_id uuid REFERENCES subscriptions",
    "ALTER TABLE invoices ADD COLUMN seat_change_to int",
    "ALTER TABLE invoices ADD COLUMN payment_link_id text",
    "ALTER TABLE invoices ADD COLUMN payment_link_url text",
    "CREATE INDEX invoices_subscription ON invoices (subscription_id) WHERE subscription_id IS NOT NULL",
    "CREATE UNIQUE INDEX invoices_payment_link ON invoices (payment_link_id) WHERE payment_link_id IS NOT NULL",
    # one renewal invoice per subscription period, even if two workers run org.renewal at once
    "CREATE UNIQUE INDEX invoices_one_per_period ON invoices (subscription_id, period_start) "
    "WHERE subscription_id IS NOT NULL AND seat_change_to IS NULL AND kind <> 'credit_note'",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in (
        "DROP INDEX IF EXISTS billing.invoices_one_per_period",
        "DROP INDEX IF EXISTS billing.invoices_payment_link",
        "DROP INDEX IF EXISTS billing.invoices_subscription",
        "ALTER TABLE billing.invoices DROP COLUMN IF EXISTS payment_link_url",
        "ALTER TABLE billing.invoices DROP COLUMN IF EXISTS payment_link_id",
        "ALTER TABLE billing.invoices DROP COLUMN IF EXISTS seat_change_to",
        "ALTER TABLE billing.invoices DROP COLUMN IF EXISTS subscription_id",
        "ALTER TABLE billing.subscriptions DROP COLUMN IF EXISTS pending_seats",
        "ALTER TABLE billing.subscriptions DROP COLUMN IF EXISTS billing_mode",
        "ALTER TABLE billing.plan_versions DROP COLUMN IF EXISTS min_seats",
    ):
        op.execute(stmt)
