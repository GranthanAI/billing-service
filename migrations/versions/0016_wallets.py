"""Pay as you Go wallets: one row per billing account that has topped up

wallets.ledger_account_id is the `liability:wallet:<billing account>` ledger account (the wallet
holds the taxable value of what was paid); monthly_cap_paise is the user's optional spend cap.
billing_app may update it (the cap), never delete a wallet.
"""

from alembic import op

revision = "0016_wallets"
down_revision = "0015_orgs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET search_path = billing")
    op.execute(
        """CREATE TABLE wallets (
  account_id         uuid PRIMARY KEY REFERENCES billing_accounts,
  ledger_account_id  uuid NOT NULL UNIQUE REFERENCES ledger_accounts,
  monthly_cap_paise  bigint CHECK (monthly_cap_paise IS NULL OR monthly_cap_paise >= 0),
  created_at         timestamptz NOT NULL DEFAULT now()
)"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON wallets TO billing_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.wallets")
