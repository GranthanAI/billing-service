"""least-privilege grants for the catalog API (publish runs as the application role)

Until now publishing a catalog used the migration role from inside the API process. The application role gets exactly what a
publish writes, nothing more: new plan versions (INSERT) whose old rows are only ever retired or given gateway plan ids
(UPDATE of those columns, never prices or limits), the picker's models table, and the two catalog tables. Plan versions stay
immutable for the application role: it cannot change a price, a limit or a checksum. No sequences are involved (uuid keys).
"""

from alembic import op

revision = "0022_catalog_publish_grants"
down_revision = "0021_catalog_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT ON billing.plan_versions TO billing_app")
    op.execute(
        "GRANT UPDATE (retired_at, razorpay_plan_month, razorpay_plan_year, gateway_kind) "
        "ON billing.plan_versions TO billing_app"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON billing.models TO billing_app")
    op.execute("GRANT SELECT, INSERT ON billing.catalog_drafts, billing.active_catalog TO billing_app")
    op.execute(
        "GRANT UPDATE (status, published_by, published_at, publish_reason) "
        "ON billing.catalog_drafts TO billing_app"
    )
    op.execute(
        "GRANT UPDATE (catalog, catalog_version, draft_id, published_by, published_at) "
        "ON billing.active_catalog TO billing_app"
    )


def downgrade() -> None:
    op.execute("REVOKE UPDATE ON billing.active_catalog FROM billing_app")
    op.execute("REVOKE UPDATE ON billing.catalog_drafts FROM billing_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON billing.models FROM billing_app")
    op.execute("REVOKE UPDATE (retired_at, razorpay_plan_month, razorpay_plan_year, gateway_kind) ON billing.plan_versions FROM billing_app")
    op.execute("REVOKE INSERT ON billing.plan_versions FROM billing_app")
