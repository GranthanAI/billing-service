"""Organisation wallet top-up requests

A staff member asks for credits for an organisation: a pending request with a Razorpay payment
link (or paid by bank transfer). `amount_paise` is what is invoiced, GST included. The request
becomes `paid` in the same transaction that records the payment, the credit lot, the ledger
posting and the B2B tax invoice. billing_app may insert and update requests, never delete them.
"""

from alembic import op

revision = "0017_org_topups"
down_revision = "0016_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET search_path = billing")
    op.execute(
        """CREATE TABLE wallet_topup_requests (
  id                  uuid PRIMARY KEY,
  billing_account_id  uuid NOT NULL REFERENCES billing_accounts,
  amount_paise        bigint NOT NULL CHECK (amount_paise > 0),
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'paid', 'cancelled')),
  gateway_link_id     text UNIQUE,
  payment_id          uuid REFERENCES payments,
  created_by          uuid NOT NULL,
  reason              text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
)"""
    )
    op.execute(
        "CREATE INDEX wallet_topup_requests_account ON wallet_topup_requests "
        "(billing_account_id, created_at DESC)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON wallet_topup_requests TO billing_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.wallet_topup_requests")
