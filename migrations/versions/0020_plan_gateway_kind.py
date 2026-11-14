"""plan_versions.gateway_kind: which gateway the stored gateway plan ids belong to

A plan id written while the in-memory fake gateway ran (plan_fake_0001) is meaningless to the simulator or to Razorpay, and
`catalog sync` used to leave it alone ("gateway plans created: 0"), so checkout failed with 502 "The id provided does not
exist". The new column says which gateway each row's ids came from; the catalog sync recreates ids that belong to another
gateway, and checkout refuses to call a gateway with a foreign id.

Additive. Backfilled from the id prefix: plan_fake_ -> fake, plan_SIM -> simulator, any other plan_ -> razorpay. Rows without ids
stay NULL. Rollback drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_plan_gateway_kind"
down_revision = "0019_payment_simulator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_versions", sa.Column("gateway_kind", sa.Text(), nullable=True), schema="billing")
    op.execute(
        """
        UPDATE billing.plan_versions SET gateway_kind = CASE
            WHEN coalesce(razorpay_plan_month, razorpay_plan_year) LIKE 'plan\_fake\_%' THEN 'fake'
            WHEN coalesce(razorpay_plan_month, razorpay_plan_year) LIKE 'plan\_SIM%' THEN 'simulator'
            WHEN coalesce(razorpay_plan_month, razorpay_plan_year) LIKE 'plan\_%' THEN 'razorpay'
            ELSE NULL END
        WHERE gateway_kind IS NULL
        """
    )
    op.create_check_constraint(
        "ck_plan_versions_gateway_kind", "plan_versions",
        "gateway_kind IS NULL OR gateway_kind IN ('fake','simulator','razorpay')", schema="billing",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plan_versions_gateway_kind", "plan_versions", schema="billing")
    op.drop_column("plan_versions", "gateway_kind", schema="billing")
