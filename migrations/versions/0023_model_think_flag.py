"""models.think: the model has a reasoning control llm-service can turn up (think longer)

Plan limits live in the plan_versions.limits JSON, so the new think_longer_per_day limit needs no column: a version stored before
it existed reads as 0 (Limits has a default). Only the picker's models table gains a column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_model_think_flag"
down_revision = "0022_catalog_publish_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column("think", sa.Boolean, nullable=False, server_default=sa.false()),
        schema="billing",
    )


def downgrade() -> None:
    op.drop_column("models", "think", schema="billing")
