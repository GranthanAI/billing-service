"""usage events (monthly partitions), daily rollup, partition function"""

from alembic import op

revision = "0004_usage"
down_revision = "0003_subscriptions"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE usage_events (
  kind               text NOT NULL,
  source_id          text NOT NULL,
  occurred_at        timestamptz NOT NULL,
  user_id            uuid NOT NULL,
  billing_account_id uuid NOT NULL,
  subscription_id    uuid,
  plan_version_id    uuid NOT NULL,
  model              text,
  model_group        model_group,
  input_tokens       int NOT NULL DEFAULT 0,
  cached_input_tokens int NOT NULL DEFAULT 0,
  output_tokens      int NOT NULL DEFAULT 0,
  aux_input_tokens   int NOT NULL DEFAULT 0,
  aux_output_tokens  int NOT NULL DEFAULT 0,
  billable_units     int NOT NULL CHECK (billable_units >= 0),
  cost_micro_inr     bigint NOT NULL CHECK (cost_micro_inr >= 0),
  price_paise        bigint NOT NULL DEFAULT 0,
  fx_usd_inr         numeric(10,4) NOT NULL,
  hold_id            text,
  PRIMARY KEY (kind, source_id, occurred_at)
) PARTITION BY RANGE (occurred_at)""",
    "CREATE INDEX usage_events_account ON usage_events (billing_account_id, occurred_at)",
    "CREATE INDEX usage_events_user    ON usage_events (user_id, occurred_at)",
    """CREATE TABLE usage_daily (
  day                date NOT NULL,
  billing_account_id uuid NOT NULL,
  user_id            uuid NOT NULL,
  model_group        model_group NOT NULL,
  messages           int NOT NULL,
  billable_units     int NOT NULL,
  cost_micro_inr     bigint NOT NULL,
  price_paise        bigint NOT NULL,
  PRIMARY KEY (day, billing_account_id, user_id, model_group)
)""",
    # SECURITY DEFINER so billing_app can create partitions without owning the table.
    """CREATE FUNCTION billing.ensure_usage_partitions(months_ahead int) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = billing, pg_temp AS $fn$
DECLARE
  base    timestamp := date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata');
  m       int;
  from_ts timestamptz;
  to_ts   timestamptz;
  part    text;
  created int := 0;
BEGIN
  FOR m IN 0..months_ahead LOOP
    from_ts := (base + make_interval(months => m)) AT TIME ZONE 'Asia/Kolkata';
    to_ts   := (base + make_interval(months => m + 1)) AT TIME ZONE 'Asia/Kolkata';
    part    := 'usage_events_' || to_char(base + make_interval(months => m), 'YYYY_MM');
    IF to_regclass('billing.' || quote_ident(part)) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE billing.%I PARTITION OF billing.usage_events FOR VALUES FROM (%L) TO (%L)',
        part, from_ts, to_ts);
      created := created + 1;
    END IF;
  END LOOP;
  RETURN created;
END
$fn$""",
    "REVOKE ALL ON FUNCTION billing.ensure_usage_partitions(int) FROM PUBLIC",
    "SELECT billing.ensure_usage_partitions(2)",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS billing.ensure_usage_partitions(int)")
    op.execute("DROP TABLE IF EXISTS billing.usage_daily")
    op.execute("DROP TABLE IF EXISTS billing.usage_events")  # drops its partitions
