"""billing_accounts: blocked_at and blocked_reason (the LLD's ACCOUNT_BLOCKED has no column)"""

from alembic import op

revision = "0008_account_block"
down_revision = "0007_guards_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET search_path = billing")
    op.execute("ALTER TABLE billing_accounts ADD COLUMN blocked_at timestamptz")
    op.execute("ALTER TABLE billing_accounts ADD COLUMN blocked_reason text")


def downgrade() -> None:
    op.execute("ALTER TABLE billing.billing_accounts DROP COLUMN IF EXISTS blocked_reason")
    op.execute("ALTER TABLE billing.billing_accounts DROP COLUMN IF EXISTS blocked_at")
