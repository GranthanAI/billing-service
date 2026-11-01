"""webhook inbox, consumer inbox, outbox, idempotency keys, jobs, admin audit"""

from alembic import op

revision = "0006_plumbing"
down_revision = "0005_money_ledger"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE webhook_inbox (
  id          bigserial PRIMARY KEY,
  gateway     text NOT NULL,
  event_id    text NOT NULL,
  event_type  text NOT NULL,
  payload     jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  status      text NOT NULL DEFAULT 'received'
              CHECK (status IN ('received','processed','ignored','failed')),
  attempts    int NOT NULL DEFAULT 0,
  next_try_at timestamptz NOT NULL DEFAULT now(),
  last_error  text,
  UNIQUE (gateway, event_id)
)""",
    "CREATE INDEX webhook_inbox_todo ON webhook_inbox (next_try_at) WHERE status = 'received'",
    """CREATE TABLE processed_events (
  consumer     text NOT NULL,
  event_id     text NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
)""",
    """CREATE TABLE outbox (
  id           bigserial PRIMARY KEY,
  topic        text NOT NULL,
  key          text NOT NULL,
  event_type   text NOT NULL,
  payload      bytea NOT NULL,
  headers      jsonb NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
)""",
    "CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL",
    """CREATE TABLE idempotency_keys (
  scope        text NOT NULL,
  key          text NOT NULL,
  request_hash text NOT NULL,
  status_code  int,
  response     jsonb,
  locked_until timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, key)
)""",
    """CREATE TABLE jobs (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}',
  dedupe_key   text UNIQUE,
  run_at       timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','running','done','dead')),
  attempts     int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 10,
  locked_until timestamptz,
  last_error   text
)""",
    "CREATE INDEX jobs_ready ON jobs (run_at) WHERE status = 'queued'",
    """CREATE TABLE admin_audit (
  id          bigserial PRIMARY KEY,
  actor_id    uuid NOT NULL,
  action      text NOT NULL,
  target_type text NOT NULL,
  target_id   text NOT NULL,
  reason      text NOT NULL,
  before      jsonb,
  after       jsonb,
  at          timestamptz NOT NULL DEFAULT now()
)""",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in (
        "admin_audit",
        "jobs",
        "idempotency_keys",
        "outbox",
        "processed_events",
        "webhook_inbox",
    ):
        op.execute(f"DROP TABLE IF EXISTS billing.{t}")
