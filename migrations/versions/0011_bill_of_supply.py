"""invoices.kind also allows 'bill_of_supply' (GST_REGISTERED=false)"""

from alembic import op

revision = "0011_bill_of_supply"
down_revision = "0010_subscription_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET search_path = billing")
    op.execute("ALTER TABLE invoices DROP CONSTRAINT invoices_kind_check")
    op.execute(
        "ALTER TABLE invoices ADD CONSTRAINT invoices_kind_check "
        "CHECK (kind IN ('tax_invoice','bill_of_supply','credit_note'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE billing.invoices DROP CONSTRAINT invoices_kind_check")
    op.execute(
        "ALTER TABLE billing.invoices ADD CONSTRAINT invoices_kind_check "
        "CHECK (kind IN ('tax_invoice','credit_note'))"
    )
