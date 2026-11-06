"""payments.fee_paise excludes the GST on the fee

Razorpay's payment `fee` includes GST (`tax` is the GST in it). Rows written before this migration
stored the whole `fee` in fee_paise next to `tax` in fee_gst_paise, which counts the GST twice.
"""

from alembic import op

revision = "0015a_fee_excludes_gst"
down_revision = "0014_ops_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE billing.payments SET fee_paise = fee_paise - fee_gst_paise "
        "WHERE fee_paise IS NOT NULL AND fee_gst_paise IS NOT NULL AND fee_paise >= fee_gst_paise"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE billing.payments SET fee_paise = fee_paise + fee_gst_paise "
        "WHERE fee_paise IS NOT NULL AND fee_gst_paise IS NOT NULL"
    )
