"""schema and enums"""

from alembic import op

revision = "0001_schema_enums"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS billing")
    op.execute("SET search_path = billing")
    op.execute("CREATE TYPE owner_type AS ENUM ('user', 'org')")
    op.execute("CREATE TYPE billing_interval AS ENUM ('month', 'year')")
    op.execute("CREATE TYPE model_group AS ENUM ('fast', 'smart', 'best', 'ultra')")
    op.execute(
        "CREATE TYPE sub_status AS ENUM ('incomplete', 'active', 'past_due', "
        "'suspended', 'cancelling', 'ended')"
    )


def downgrade() -> None:
    # The schema stays: it holds alembic_version.
    for t in ("sub_status", "model_group", "billing_interval", "owner_type"):
        op.execute(f"DROP TYPE IF EXISTS billing.{t}")
