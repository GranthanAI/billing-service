"""catalog and accounts"""

from alembic import op

revision = "0002_catalog_accounts"
down_revision = "0001_schema_enums"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE plan_versions (
  id                 uuid PRIMARY KEY,
  plan_code          text NOT NULL,
  version            int  NOT NULL,
  rank               int  NOT NULL,
  variant            text,
  price_month_paise  bigint CHECK (price_month_paise >= 0),
  price_year_paise   bigint CHECK (price_year_paise >= 0),
  limits             jsonb NOT NULL,
  allowed_models     text[] NOT NULL,
  razorpay_plan_month text,
  razorpay_plan_year  text,
  effective_from     timestamptz NOT NULL,
  retired_at         timestamptz,
  checksum           text NOT NULL,
  UNIQUE (plan_code, variant, version)
)""",
    """CREATE TABLE model_prices (
  model                 text NOT NULL,
  provider              text NOT NULL,
  model_group           model_group NOT NULL,
  input_usd_per_m       numeric(12,6) NOT NULL,
  cached_input_usd_per_m numeric(12,6),
  output_usd_per_m      numeric(12,6) NOT NULL,
  effective_from        timestamptz NOT NULL,
  PRIMARY KEY (model, effective_from)
)""",
    """CREATE TABLE fx_rates (
  effective_from date PRIMARY KEY,
  usd_inr        numeric(10,4) NOT NULL CHECK (usd_inr > 0),
  source         text NOT NULL
)""",
    """CREATE TABLE billing_accounts (
  id                  uuid PRIMARY KEY,
  owner_type          owner_type NOT NULL,
  owner_id            uuid NOT NULL,
  legal_name          text,
  gstin               text CHECK (gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
  state_code          char(2),
  billing_email       text NOT NULL,
  gateway_customer_id text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_type, owner_id)
)""",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in ("billing_accounts", "fx_rates", "model_prices", "plan_versions"):
        op.execute(f"DROP TABLE IF EXISTS billing.{t}")
