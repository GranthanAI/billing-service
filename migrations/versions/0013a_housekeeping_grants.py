"""billing_app may delete old webhook_inbox rows (the housekeeping job prunes them)

Numbered 0013a so the later migrations keep their planned numbers (0014 ops tables, 0015 orgs,
0016 wallets).
"""

from alembic import op

revision = "0013a_housekeeping_grants"
down_revision = "0013_nullable_billing_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON billing.webhook_inbox TO billing_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON billing.webhook_inbox FROM billing_app")
