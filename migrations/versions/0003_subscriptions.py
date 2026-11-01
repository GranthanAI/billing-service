"""subscriptions, history, overrides"""

from alembic import op

revision = "0003_subscriptions"
down_revision = "0002_catalog_accounts"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE subscriptions (
  id                           uuid PRIMARY KEY,
  billing_account_id           uuid NOT NULL REFERENCES billing_accounts,
  plan_version_id              uuid NOT NULL REFERENCES plan_versions,
  interval                     billing_interval NOT NULL,
  seats                        int  NOT NULL DEFAULT 1 CHECK (seats >= 1),
  status                       sub_status NOT NULL,
  gateway                      text NOT NULL DEFAULT 'razorpay',
  gateway_subscription_id      text UNIQUE,
  next_gateway_subscription_id text UNIQUE,
  current_period_start         timestamptz,
  current_period_end           timestamptz,
  grace_until                  timestamptz,
  pending_plan_version_id      uuid REFERENCES plan_versions,
  pending_interval             billing_interval,
  last_period_paid_paise       bigint,
  lock_version                 int NOT NULL DEFAULT 0,
  created_at                   timestamptz NOT NULL DEFAULT now(),
  updated_at                   timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE UNIQUE INDEX subscriptions_one_live ON subscriptions (billing_account_id)
  WHERE status IN ('active', 'past_due', 'suspended', 'cancelling')""",
    "CREATE INDEX subscriptions_due ON subscriptions (status, grace_until, current_period_end)",
    """CREATE TABLE subscription_history (
  id              bigserial PRIMARY KEY,
  subscription_id uuid NOT NULL REFERENCES subscriptions,
  from_status     sub_status,
  to_status       sub_status NOT NULL,
  trigger         text NOT NULL,
  cause_ref       text,
  at              timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE plan_overrides (
  id              uuid PRIMARY KEY,
  owner_type      owner_type NOT NULL,
  owner_id        uuid NOT NULL,
  plan_version_id uuid NOT NULL REFERENCES plan_versions,
  reason          text NOT NULL,
  starts_at       timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL CHECK (expires_at > starts_at),
  ended_at        timestamptz,
  granted_by      uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE INDEX plan_overrides_owner ON plan_overrides (owner_type, owner_id)
  WHERE ended_at IS NULL""",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in ("plan_overrides", "subscription_history", "subscriptions"):
        op.execute(f"DROP TABLE IF EXISTS billing.{t}")
