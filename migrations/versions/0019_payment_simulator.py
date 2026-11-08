"""payment simulator: schema billing_sim, billing.gateway_mode and billing.payments.gateway

`billing_sim` is the persistent, Razorpay-shaped provider behind PAYMENT_SIMULATION=true (plan
section 5.3). It is always created and stays empty unless the simulator runs. `gateway_mode` holds
the one row the start-up guard compares against, so simulated and real money are never mixed in
one database. `payments.gateway` says which provider took each payment.
"""

from alembic import op

revision = "0019_payment_simulator"
down_revision = "0018_storage_shortfall"
branch_labels = None
depends_on = None

STATEMENTS = (
    "CREATE SCHEMA billing_sim",
    """CREATE TABLE billing_sim.sim_plans (
  id          text PRIMARY KEY,
  period      text NOT NULL,
  interval    integer NOT NULL DEFAULT 1,
  amount      bigint NOT NULL CHECK (amount >= 0),
  currency    text NOT NULL DEFAULT 'INR',
  name        text NOT NULL,
  notes       jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE billing_sim.sim_subscriptions (
  id                   text PRIMARY KEY,
  plan_id              text NOT NULL REFERENCES billing_sim.sim_plans,
  status               text NOT NULL CHECK (status IN ('created', 'authenticated', 'active',
                         'pending', 'halted', 'cancelled', 'completed', 'paused', 'expired')),
  current_start        timestamptz,
  current_end          timestamptz,
  charge_at            timestamptz,
  start_at             timestamptz,
  paid_count           integer NOT NULL DEFAULT 0,
  total_count          integer NOT NULL,
  notes                jsonb NOT NULL DEFAULT '{}',
  billing_ref          text,
  short_url            text,
  method               text,
  next_outcome         text NOT NULL DEFAULT 'success'
                         CHECK (next_outcome IN ('success', 'fail', 'halt')),
  cancel_at_cycle_end  boolean NOT NULL DEFAULT false,
  retry_count          integer NOT NULL DEFAULT 0,
  next_retry_at        timestamptz,
  notice_sent_for      timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  ended_at             timestamptz
)""",
    "CREATE UNIQUE INDEX sim_subscriptions_ref ON billing_sim.sim_subscriptions "
    "(plan_id, billing_ref) WHERE billing_ref IS NOT NULL",
    "CREATE INDEX sim_subscriptions_due ON billing_sim.sim_subscriptions (charge_at) "
    "WHERE status IN ('authenticated', 'active', 'pending')",
    """CREATE TABLE billing_sim.sim_payments (
  id                 text PRIMARY KEY,
  amount             bigint NOT NULL CHECK (amount >= 0),
  currency           text NOT NULL DEFAULT 'INR',
  status             text NOT NULL CHECK (status IN ('created', 'authorized', 'captured',
                       'failed', 'refunded')),
  method             text,
  fee                bigint,
  tax                bigint,
  order_id           text,
  invoice_id         text,
  subscription_id    text,
  link_id            text,
  description        text,
  notes              jsonb NOT NULL DEFAULT '{}',
  error_code         text,
  error_description  text,
  amount_refunded    bigint NOT NULL DEFAULT 0,
  created_at         timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX sim_payments_sub ON billing_sim.sim_payments (subscription_id)",
    "CREATE INDEX sim_payments_created ON billing_sim.sim_payments (created_at)",
    """CREATE TABLE billing_sim.sim_refunds (
  id             text PRIMARY KEY,
  payment_id     text NOT NULL REFERENCES billing_sim.sim_payments,
  amount         bigint NOT NULL CHECK (amount > 0),
  status         text NOT NULL CHECK (status IN ('pending', 'processed', 'failed')),
  notes          jsonb NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  process_after  timestamptz NOT NULL DEFAULT now(),
  processed_at   timestamptz
)""",
    """CREATE TABLE billing_sim.sim_orders (
  id          text PRIMARY KEY,
  amount      bigint NOT NULL CHECK (amount >= 0),
  receipt     text UNIQUE,
  notes       jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL CHECK (status IN ('created', 'attempted', 'paid')),
  created_at  timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE billing_sim.sim_payment_links (
  id              text PRIMARY KEY,
  amount          bigint NOT NULL CHECK (amount >= 0),
  reference_id    text UNIQUE,
  status          text NOT NULL CHECK (status IN ('created', 'paid', 'cancelled', 'expired')),
  description     text,
  customer_name   text,
  customer_email  text,
  notes           jsonb NOT NULL DEFAULT '{}',
  short_url       text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE billing_sim.sim_settlements (
  id          text PRIMARY KEY,
  day         date NOT NULL,
  gross       bigint NOT NULL,
  fee         bigint NOT NULL,
  tax         bigint NOT NULL,
  net         bigint NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE billing_sim.sim_settlement_items (
  id             bigserial PRIMARY KEY,
  settlement_id  text NOT NULL REFERENCES billing_sim.sim_settlements,
  entity_id      text NOT NULL UNIQUE,
  kind           text NOT NULL CHECK (kind IN ('payment', 'refund')),
  amount         bigint NOT NULL,
  credit         bigint NOT NULL DEFAULT 0,
  debit          bigint NOT NULL DEFAULT 0,
  fee            bigint NOT NULL DEFAULT 0,
  tax            bigint NOT NULL DEFAULT 0,
  payment_id     text,
  settled_at     timestamptz NOT NULL
)""",
    """CREATE TABLE billing_sim.sim_webhook_outbox (
  id             bigserial PRIMARY KEY,
  event_id       text NOT NULL,
  event          text NOT NULL,
  body           bytea NOT NULL,
  signature      text NOT NULL,
  status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'delivered', 'dropped', 'failed')),
  deliver_after  timestamptz NOT NULL,
  attempts       integer NOT NULL DEFAULT 0,
  last_error     text,
  delivered_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX sim_webhook_outbox_due ON billing_sim.sim_webhook_outbox (deliver_after) "
    "WHERE status = 'pending'",
    """CREATE TABLE billing.gateway_mode (
  singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  mode       text NOT NULL,
  set_at     timestamptz NOT NULL DEFAULT now()
)""",
    "ALTER TABLE billing.payments ADD COLUMN gateway text NOT NULL DEFAULT 'razorpay'",
    "GRANT SELECT, INSERT, UPDATE ON billing.gateway_mode TO billing_app",
    "GRANT USAGE ON SCHEMA billing_sim TO billing_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA billing_sim TO billing_app",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA billing_sim TO billing_app",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE billing.payments DROP COLUMN IF EXISTS gateway")
    op.execute("DROP TABLE IF EXISTS billing.gateway_mode")
    op.execute("DROP SCHEMA IF EXISTS billing_sim CASCADE")
