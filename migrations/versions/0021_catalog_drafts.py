"""catalog drafts and the published catalog (admin catalog API)

`catalog_drafts`: a proposed catalog (the plans.yaml schema, as JSON) with its diff, impact and warnings, until a billing_admin
publishes it. `active_catalog`: at most one row, the catalog published from a draft; when it exists it wins over config/plans.yaml
(which stays the seed). Additive. Rollback drops both tables (plan_versions already written by a publish stay, as they do after any
catalog sync).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0021_catalog_drafts"
down_revision = "0020_plan_gateway_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_drafts",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("based_on_version", sa.Text, nullable=False),
        sa.Column("catalog", pg.JSONB, nullable=False),
        sa.Column("note", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("diff", pg.JSONB, nullable=False),
        sa.Column("impact", pg.JSONB, nullable=False),
        sa.Column("warnings", pg.JSONB, nullable=False),
        sa.Column("created_by", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("published_by", sa.Uuid),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_reason", sa.Text),
        sa.CheckConstraint("status IN ('open','published')", name="ck_catalog_drafts_status"),
        schema="billing",
    )
    op.create_table(
        "active_catalog",
        sa.Column("id", sa.SmallInteger, primary_key=True, server_default=sa.text("1")),
        sa.Column("catalog", pg.JSONB, nullable=False),
        sa.Column("catalog_version", sa.Text, nullable=False),
        sa.Column("draft_id", sa.Uuid, nullable=False),
        sa.Column("published_by", sa.Uuid, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_active_catalog_single"),
        schema="billing",
    )


def downgrade() -> None:
    op.drop_table("active_catalog", schema="billing")
    op.drop_table("catalog_drafts", schema="billing")
