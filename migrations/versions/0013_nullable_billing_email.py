"""billing_accounts.billing_email is optional: Free users get an account lazily, without one"""

from alembic import op

revision = "0013_nullable_billing_email"
down_revision = "0012_catalog_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE billing.billing_accounts ALTER COLUMN billing_email DROP NOT NULL")


def downgrade() -> None:
    # accounts created without an email get a placeholder so the constraint can come back
    op.execute(
        "UPDATE billing.billing_accounts SET billing_email = 'unknown@invalid' "
        "WHERE billing_email IS NULL"
    )
    op.execute("ALTER TABLE billing.billing_accounts ALTER COLUMN billing_email SET NOT NULL")
