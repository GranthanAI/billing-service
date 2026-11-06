"""reconciliation_runs and provider_costs (Phase 9)"""

from alembic import op

revision = "0014_ops_tables"
down_revision = "0013a_housekeeping_grants"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE reconciliation_runs (
  id               bigserial PRIMARY KEY,
  run_date         date NOT NULL,
  "check"          text NOT NULL,
  status           text NOT NULL CHECK (status IN ('ok','difference','error')),
  difference_paise bigint NOT NULL DEFAULT 0,
  details          jsonb NOT NULL DEFAULT '{}',
  created_at       timestamptz NOT NULL DEFAULT now()
)""",
    'CREATE INDEX reconciliation_runs_date ON reconciliation_runs (run_date, "check")',
    """CREATE TABLE provider_costs (
  provider         text NOT NULL,
  day              date NOT NULL,
  cost_micro_inr   bigint NOT NULL CHECK (cost_micro_inr >= 0),
  source           text NOT NULL,
  imported_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, day)
)""",
    "GRANT SELECT, INSERT ON reconciliation_runs TO billing_app",
    "GRANT SELECT, INSERT, UPDATE ON provider_costs TO billing_app",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.provider_costs")
    op.execute("DROP TABLE IF EXISTS billing.reconciliation_runs")
