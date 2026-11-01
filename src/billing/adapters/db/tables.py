"""SQLAlchemy Core tables. Must match migrations/versions (checked by a compare_metadata test)."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

metadata = sa.MetaData(schema="billing")

owner_type = pg.ENUM("user", "org", name="owner_type", create_type=False, metadata=metadata)
billing_interval = pg.ENUM(
    "month", "year", name="billing_interval", create_type=False, metadata=metadata
)
model_group = pg.ENUM(
    "fast", "smart", "best", "ultra", name="model_group", create_type=False, metadata=metadata
)
sub_status = pg.ENUM(
    "incomplete",
    "active",
    "past_due",
    "suspended",
    "cancelling",
    "ended",
    name="sub_status",
    create_type=False,
    metadata=metadata,
)

_NOW = sa.text("now()")


def _ts(name: str, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


plan_versions = sa.Table(
    "plan_versions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("plan_code", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("rank", sa.Integer, nullable=False),
    sa.Column("variant", sa.Text),
    sa.Column("price_month_paise", sa.BigInteger),
    sa.Column("price_year_paise", sa.BigInteger),
    sa.Column("limits", pg.JSONB, nullable=False),
    sa.Column("allowed_models", pg.ARRAY(sa.Text), nullable=False),
    sa.Column("razorpay_plan_month", sa.Text),
    sa.Column("razorpay_plan_year", sa.Text),
    _ts("effective_from", nullable=False),
    _ts("retired_at"),
    sa.Column("checksum", sa.Text, nullable=False),
    sa.UniqueConstraint("plan_code", "variant", "version"),
    sa.CheckConstraint("price_month_paise >= 0"),
    sa.CheckConstraint("price_year_paise >= 0"),
)

model_prices = sa.Table(
    "model_prices",
    metadata,
    sa.Column("model", sa.Text, primary_key=True),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model_group", model_group, nullable=False),
    sa.Column("input_usd_per_m", sa.Numeric(12, 6), nullable=False),
    sa.Column("cached_input_usd_per_m", sa.Numeric(12, 6)),
    sa.Column("output_usd_per_m", sa.Numeric(12, 6), nullable=False),
    _ts("effective_from", primary_key=True, nullable=False),
)

fx_rates = sa.Table(
    "fx_rates",
    metadata,
    sa.Column("effective_from", sa.Date, primary_key=True),
    sa.Column("usd_inr", sa.Numeric(10, 4), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.CheckConstraint("usd_inr > 0"),
)

billing_accounts = sa.Table(
    "billing_accounts",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_type", owner_type, nullable=False),
    sa.Column("owner_id", sa.Uuid, nullable=False),
    sa.Column("legal_name", sa.Text),
    sa.Column("gstin", sa.Text),
    sa.Column("state_code", sa.CHAR(2)),
    sa.Column("billing_email", sa.Text, nullable=False),
    sa.Column("gateway_customer_id", sa.Text),
    _ts("created_at", nullable=False, server_default=_NOW),
    sa.UniqueConstraint("owner_type", "owner_id"),
)

subscriptions = sa.Table(
    "subscriptions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "billing_account_id", sa.Uuid, sa.ForeignKey("billing.billing_accounts.id"), nullable=False
    ),
    sa.Column(
        "plan_version_id", sa.Uuid, sa.ForeignKey("billing.plan_versions.id"), nullable=False
    ),
    sa.Column("interval", billing_interval, nullable=False),
    sa.Column("seats", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("status", sub_status, nullable=False),
    sa.Column("gateway", sa.Text, nullable=False, server_default=sa.text("'razorpay'")),
    sa.Column("gateway_subscription_id", sa.Text, unique=True),
    sa.Column("next_gateway_subscription_id", sa.Text, unique=True),
    _ts("current_period_start"),
    _ts("current_period_end"),
    _ts("grace_until"),
    sa.Column("pending_plan_version_id", sa.Uuid, sa.ForeignKey("billing.plan_versions.id")),
    sa.Column("pending_interval", billing_interval),
    sa.Column("last_period_paid_paise", sa.BigInteger),
    sa.Column("lock_version", sa.Integer, nullable=False, server_default=sa.text("0")),
    _ts("created_at", nullable=False, server_default=_NOW),
    _ts("updated_at", nullable=False, server_default=_NOW),
    sa.Index(
        "subscriptions_one_live",
        "billing_account_id",
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'past_due', 'suspended', 'cancelling')"),
    ),
    sa.Index("subscriptions_due", "status", "grace_until", "current_period_end"),
)

subscription_history = sa.Table(
    "subscription_history",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "subscription_id", sa.Uuid, sa.ForeignKey("billing.subscriptions.id"), nullable=False
    ),
    sa.Column("from_status", sub_status),
    sa.Column("to_status", sub_status, nullable=False),
    sa.Column("trigger", sa.Text, nullable=False),
    sa.Column("cause_ref", sa.Text),
    _ts("at", nullable=False, server_default=_NOW),
)

plan_overrides = sa.Table(
    "plan_overrides",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_type", owner_type, nullable=False),
    sa.Column("owner_id", sa.Uuid, nullable=False),
    sa.Column(
        "plan_version_id", sa.Uuid, sa.ForeignKey("billing.plan_versions.id"), nullable=False
    ),
    sa.Column("reason", sa.Text, nullable=False),
    _ts("starts_at", nullable=False),
    _ts("expires_at", nullable=False),
    _ts("ended_at"),
    sa.Column("granted_by", sa.Uuid, nullable=False),
    _ts("created_at", nullable=False, server_default=_NOW),
    sa.Index(
        "plan_overrides_owner",
        "owner_type",
        "owner_id",
        postgresql_where=sa.text("ended_at IS NULL"),
    ),
)

usage_events = sa.Table(
    "usage_events",
    metadata,
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, primary_key=True),
    _ts("occurred_at", primary_key=True, nullable=False),
    sa.Column("user_id", sa.Uuid, nullable=False),
    sa.Column("billing_account_id", sa.Uuid, nullable=False),
    sa.Column("subscription_id", sa.Uuid),
    sa.Column("plan_version_id", sa.Uuid, nullable=False),
    sa.Column("model", sa.Text),
    sa.Column("model_group", model_group),
    sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("cached_input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("aux_input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("aux_output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("billable_units", sa.Integer, nullable=False),
    sa.Column("cost_micro_inr", sa.BigInteger, nullable=False),
    sa.Column("price_paise", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("fx_usd_inr", sa.Numeric(10, 4), nullable=False),
    sa.Column("hold_id", sa.Text),
    sa.Index("usage_events_account", "billing_account_id", "occurred_at"),
    sa.Index("usage_events_user", "user_id", "occurred_at"),
    postgresql_partition_by="RANGE (occurred_at)",
)

usage_daily = sa.Table(
    "usage_daily",
    metadata,
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("billing_account_id", sa.Uuid, primary_key=True),
    sa.Column("user_id", sa.Uuid, primary_key=True),
    sa.Column("model_group", model_group, primary_key=True),
    sa.Column("messages", sa.Integer, nullable=False),
    sa.Column("billable_units", sa.Integer, nullable=False),
    sa.Column("cost_micro_inr", sa.BigInteger, nullable=False),
    sa.Column("price_paise", sa.BigInteger, nullable=False),
)

payments = sa.Table(
    "payments",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "billing_account_id", sa.Uuid, sa.ForeignKey("billing.billing_accounts.id"), nullable=False
    ),
    sa.Column("subscription_id", sa.Uuid, sa.ForeignKey("billing.subscriptions.id")),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("gateway_payment_id", sa.Text, nullable=False, unique=True),
    sa.Column("gateway_order_id", sa.Text),
    sa.Column("method", sa.Text),
    sa.Column("amount_paise", sa.BigInteger, nullable=False),
    sa.Column("fee_paise", sa.BigInteger),
    sa.Column("fee_gst_paise", sa.BigInteger),
    sa.Column("status", sa.Text, nullable=False),
    _ts("captured_at", nullable=False),
    sa.Column("raw", pg.JSONB, nullable=False),
)

refunds = sa.Table(
    "refunds",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("payment_id", sa.Uuid, sa.ForeignKey("billing.payments.id"), nullable=False),
    sa.Column("gateway_refund_id", sa.Text, unique=True),
    sa.Column("amount_paise", sa.BigInteger, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    _ts("created_at", nullable=False, server_default=_NOW),
)

invoice_counters = sa.Table(
    "invoice_counters",
    metadata,
    sa.Column("series", sa.Text, primary_key=True),
    sa.Column("financial_year", sa.CHAR(4), primary_key=True),
    sa.Column("next_number", sa.Integer, nullable=False, server_default=sa.text("1")),
)

invoices = sa.Table(
    "invoices",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("number", sa.Text, nullable=False, unique=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("original_invoice_id", sa.Uuid, sa.ForeignKey("billing.invoices.id")),
    sa.Column(
        "billing_account_id", sa.Uuid, sa.ForeignKey("billing.billing_accounts.id"), nullable=False
    ),
    sa.Column("payment_id", sa.Uuid, sa.ForeignKey("billing.payments.id")),
    _ts("issued_at", nullable=False),
    _ts("period_start"),
    _ts("period_end"),
    sa.Column("customer_name", sa.Text),
    sa.Column("customer_gstin", sa.Text),
    sa.Column("customer_state_code", sa.CHAR(2), nullable=False),
    sa.Column("supplier_state_code", sa.CHAR(2), nullable=False),
    sa.Column("taxable_paise", sa.BigInteger, nullable=False),
    sa.Column("cgst_paise", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("sgst_paise", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("igst_paise", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("total_paise", sa.BigInteger, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("pdf_object_key", sa.Text),
)

invoice_lines = sa.Table(
    "invoice_lines",
    metadata,
    sa.Column("invoice_id", sa.Uuid, sa.ForeignKey("billing.invoices.id"), primary_key=True),
    sa.Column("line_no", sa.Integer, primary_key=True),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("sac_code", sa.Text, nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("unit_price_paise", sa.BigInteger, nullable=False),
    sa.Column("taxable_paise", sa.BigInteger, nullable=False),
)

ledger_accounts = sa.Table(
    "ledger_accounts",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("billing_account_id", sa.Uuid, sa.ForeignKey("billing.billing_accounts.id")),
)

ledger_transactions = sa.Table(
    "ledger_transactions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("ref_type", sa.Text, nullable=False),
    sa.Column("ref_id", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
    _ts("occurred_at", nullable=False),
    _ts("created_at", nullable=False, server_default=_NOW),
)

ledger_entries = sa.Table(
    "ledger_entries",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "transaction_id", sa.Uuid, sa.ForeignKey("billing.ledger_transactions.id"), nullable=False
    ),
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("billing.ledger_accounts.id"), nullable=False),
    sa.Column("amount_paise", sa.BigInteger, nullable=False),
    sa.Index("ledger_entries_account", "account_id", "id"),
)

ledger_balances = sa.Table(
    "ledger_balances",
    metadata,
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("billing.ledger_accounts.id"), primary_key=True),
    sa.Column("balance_paise", sa.BigInteger, nullable=False),
    sa.Column("last_entry_id", sa.BigInteger, nullable=False),
)

credit_lots = sa.Table(
    "credit_lots",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "wallet_account_id", sa.Uuid, sa.ForeignKey("billing.ledger_accounts.id"), nullable=False
    ),
    sa.Column("payment_id", sa.Uuid, sa.ForeignKey("billing.payments.id"), nullable=False),
    sa.Column("amount_paise", sa.BigInteger, nullable=False),
    sa.Column("remaining_paise", sa.BigInteger, nullable=False),
    _ts("expires_at", nullable=False),
    sa.Index(
        "credit_lots_spend",
        "wallet_account_id",
        "expires_at",
        postgresql_where=sa.text("remaining_paise > 0"),
    ),
)

webhook_inbox = sa.Table(
    "webhook_inbox",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("gateway", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("payload", pg.JSONB, nullable=False),
    _ts("received_at", nullable=False, server_default=_NOW),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'received'")),
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    _ts("next_try_at", nullable=False, server_default=_NOW),
    sa.Column("last_error", sa.Text),
    sa.UniqueConstraint("gateway", "event_id"),
    sa.Index("webhook_inbox_todo", "next_try_at", postgresql_where=sa.text("status = 'received'")),
)

processed_events = sa.Table(
    "processed_events",
    metadata,
    sa.Column("consumer", sa.Text, primary_key=True),
    sa.Column("event_id", sa.Text, primary_key=True),
    _ts("processed_at", nullable=False, server_default=_NOW),
)

outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("topic", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("payload", sa.LargeBinary, nullable=False),
    sa.Column("headers", pg.JSONB, nullable=False, server_default=sa.text("'{}'")),
    _ts("created_at", nullable=False, server_default=_NOW),
    _ts("published_at"),
    sa.Index("outbox_unpublished", "id", postgresql_where=sa.text("published_at IS NULL")),
)

idempotency_keys = sa.Table(
    "idempotency_keys",
    metadata,
    sa.Column("scope", sa.Text, primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("status_code", sa.Integer),
    sa.Column("response", pg.JSONB),
    _ts("locked_until"),
    _ts("created_at", nullable=False, server_default=_NOW),
)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", pg.JSONB, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("dedupe_key", sa.Text, unique=True),
    _ts("run_at", nullable=False, server_default=_NOW),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("10")),
    _ts("locked_until"),
    sa.Column("last_error", sa.Text),
    sa.Index("jobs_ready", "run_at", postgresql_where=sa.text("status = 'queued'")),
)

admin_audit = sa.Table(
    "admin_audit",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("actor_id", sa.Uuid, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_type", sa.Text, nullable=False),
    sa.Column("target_id", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("before", pg.JSONB),
    sa.Column("after", pg.JSONB),
    _ts("at", nullable=False, server_default=_NOW),
)
