"""subscription_changes: a plan change waiting for a new mandate to be authorised"""

from alembic import op

revision = "0010_subscription_changes"
down_revision = "0008_account_block"  # 0009 is not in this repository
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE subscription_changes (
  id                      uuid PRIMARY KEY,
  subscription_id         uuid NOT NULL REFERENCES subscriptions,
  kind                    text NOT NULL CHECK (kind IN ('upgrade','downgrade','resume')),
  new_plan_version_id     uuid NOT NULL REFERENCES plan_versions,
  new_interval            billing_interval NOT NULL,
  gateway_subscription_id text NOT NULL UNIQUE,
  status                  text NOT NULL CHECK (status IN ('pending','authorised','applied','abandoned')),
  refund_payment_id       uuid REFERENCES payments,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE UNIQUE INDEX subscription_changes_one_pending ON subscription_changes (subscription_id)
  WHERE status IN ('pending','authorised')""",
    "GRANT SELECT, INSERT, UPDATE ON subscription_changes TO billing_app",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.subscription_changes")
