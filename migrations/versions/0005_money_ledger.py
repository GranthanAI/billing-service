"""payments, invoices, ledger, credit lots"""

from alembic import op

revision = "0005_money_ledger"
down_revision = "0004_usage"
branch_labels = None
depends_on = None

# (code, kind): exactly the account codes in billing.domain.ledger
SYSTEM_ACCOUNTS = [
    ("asset:razorpay_clearing", "asset"),
    ("asset:bank", "asset"),
    ("asset:gst_input", "asset"),
    ("liability:gst_output:cgst", "liability"),
    ("liability:gst_output:sgst", "liability"),
    ("liability:gst_output:igst", "liability"),
    ("liability:deferred_revenue", "liability"),
    ("revenue:subscription", "revenue"),
    ("revenue:payg", "revenue"),
    ("revenue:breakage", "revenue"),
    ("expense:gateway_fees", "expense"),
]

STATEMENTS = [
    """CREATE TABLE payments (
  id                 uuid PRIMARY KEY,
  billing_account_id uuid NOT NULL REFERENCES billing_accounts,
  subscription_id    uuid REFERENCES subscriptions,
  purpose            text NOT NULL CHECK (purpose IN ('subscription','upgrade','topup','org_invoice')),
  gateway_payment_id text NOT NULL UNIQUE,
  gateway_order_id   text,
  method             text,
  amount_paise       bigint NOT NULL CHECK (amount_paise > 0),
  fee_paise          bigint,
  fee_gst_paise      bigint,
  status             text NOT NULL CHECK (status IN ('captured','refunded','partially_refunded')),
  captured_at        timestamptz NOT NULL,
  raw                jsonb NOT NULL
)""",
    """CREATE TABLE refunds (
  id                uuid PRIMARY KEY,
  payment_id        uuid NOT NULL REFERENCES payments,
  gateway_refund_id text UNIQUE,
  amount_paise      bigint NOT NULL CHECK (amount_paise > 0),
  reason            text NOT NULL,
  status            text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE invoice_counters (
  series         text NOT NULL,
  financial_year char(4) NOT NULL,
  next_number    int NOT NULL DEFAULT 1,
  PRIMARY KEY (series, financial_year)
)""",
    """CREATE TABLE invoices (
  id                   uuid PRIMARY KEY,
  number               text NOT NULL UNIQUE CHECK (length(number) <= 16),
  kind                 text NOT NULL CHECK (kind IN ('tax_invoice','credit_note')),
  original_invoice_id  uuid REFERENCES invoices,
  billing_account_id   uuid NOT NULL REFERENCES billing_accounts,
  payment_id           uuid REFERENCES payments,
  issued_at            timestamptz NOT NULL,
  period_start         timestamptz,
  period_end           timestamptz,
  customer_name        text,
  customer_gstin       text,
  customer_state_code  char(2) NOT NULL,
  supplier_state_code  char(2) NOT NULL,
  taxable_paise        bigint NOT NULL,
  cgst_paise           bigint NOT NULL DEFAULT 0,
  sgst_paise           bigint NOT NULL DEFAULT 0,
  igst_paise           bigint NOT NULL DEFAULT 0,
  total_paise          bigint NOT NULL,
  status               text NOT NULL CHECK (status IN ('issued','paid','void')),
  pdf_object_key       text,
  CHECK (taxable_paise + cgst_paise + sgst_paise + igst_paise = total_paise)
)""",
    """CREATE TABLE invoice_lines (
  invoice_id       uuid NOT NULL REFERENCES invoices,
  line_no          int  NOT NULL,
  description      text NOT NULL,
  sac_code         text NOT NULL,
  quantity         int  NOT NULL,
  unit_price_paise bigint NOT NULL,
  taxable_paise    bigint NOT NULL,
  PRIMARY KEY (invoice_id, line_no)
)""",
    """CREATE TABLE ledger_accounts (
  id                 uuid PRIMARY KEY,
  code               text NOT NULL UNIQUE,
  kind               text NOT NULL CHECK (kind IN ('asset','liability','revenue','expense')),
  billing_account_id uuid REFERENCES billing_accounts
)""",
    """CREATE TABLE ledger_transactions (
  id              uuid PRIMARY KEY,
  kind            text NOT NULL,
  ref_type        text NOT NULL,
  ref_id          text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  occurred_at     timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE ledger_entries (
  id             bigserial PRIMARY KEY,
  transaction_id uuid NOT NULL REFERENCES ledger_transactions,
  account_id     uuid NOT NULL REFERENCES ledger_accounts,
  amount_paise   bigint NOT NULL CHECK (amount_paise <> 0)
)""",
    "CREATE INDEX ledger_entries_account ON ledger_entries (account_id, id)",
    """CREATE TABLE ledger_balances (
  account_id    uuid PRIMARY KEY REFERENCES ledger_accounts,
  balance_paise bigint NOT NULL,
  last_entry_id bigint NOT NULL
)""",
    """CREATE TABLE credit_lots (
  id                uuid PRIMARY KEY,
  wallet_account_id uuid NOT NULL REFERENCES ledger_accounts,
  payment_id        uuid NOT NULL REFERENCES payments,
  amount_paise      bigint NOT NULL CHECK (amount_paise > 0),
  remaining_paise   bigint NOT NULL CHECK (remaining_paise >= 0),
  expires_at        timestamptz NOT NULL
)""",
    """CREATE INDEX credit_lots_spend ON credit_lots (wallet_account_id, expires_at)
  WHERE remaining_paise > 0""",
]


def upgrade() -> None:
    op.execute("SET search_path = billing")
    for stmt in STATEMENTS:
        op.execute(stmt)
    for code, kind in SYSTEM_ACCOUNTS:
        op.execute(
            f"INSERT INTO ledger_accounts (id, code, kind) "
            f"VALUES (gen_random_uuid(), '{code}', '{kind}')"
        )


def downgrade() -> None:
    for t in (
        "credit_lots",
        "ledger_balances",
        "ledger_entries",
        "ledger_transactions",
        "ledger_accounts",
        "invoice_lines",
        "invoices",
        "invoice_counters",
        "refunds",
        "payments",
    ):
        op.execute(f"DROP TABLE IF EXISTS billing.{t}")
