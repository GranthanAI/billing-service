"""plan_versions.default_group (Auto) and the models table (the picker list)"""

from alembic import op

revision = "0012_catalog_models"
down_revision = "0011_bill_of_supply"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE plan_versions ADD COLUMN default_group model_group",
    # existing versions: Free is Fast, every other plan is Smart (the catalog's defaults)
    "UPDATE plan_versions SET default_group = CASE WHEN plan_code = 'free' THEN 'fast' "
    "ELSE 'smart' END::model_group",
    "ALTER TABLE plan_versions ALTER COLUMN default_group SET NOT NULL",
    """CREATE TABLE models (
  id          text PRIMARY KEY,
  label       text NOT NULL,
  provider    text NOT NULL,
  model_group model_group NOT NULL,
  sort_order  int NOT NULL
)""",
    # like plan_versions, only the migrator (catalog sync) writes it
    "REVOKE INSERT, UPDATE, DELETE ON models FROM billing_app",
    "GRANT SELECT ON models TO billing_app",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.models")
    op.execute("ALTER TABLE billing.plan_versions DROP COLUMN IF EXISTS default_group")
