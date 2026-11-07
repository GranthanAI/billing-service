"""usage_events.shortfall_taxable_paise: what a storage charge could not take from the wallet

The monthly `storage.bill` job debits what the wallet holds (it never goes negative) and records
the rest on the usage row, so it can be collected after a top-up. Always 0 for other usage kinds.
"""

from alembic import op

revision = "0018_storage_shortfall"
down_revision = "0017_org_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE billing.usage_events "
        "ADD COLUMN shortfall_taxable_paise bigint NOT NULL DEFAULT 0 "
        "CHECK (shortfall_taxable_paise >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE billing.usage_events DROP COLUMN IF EXISTS shortfall_taxable_paise")
