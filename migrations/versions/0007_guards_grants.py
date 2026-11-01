"""append-only guards, balanced-ledger trigger, grants for billing_app"""

from alembic import op

revision = "0007_guards_grants"
down_revision = "0006_plumbing"
branch_labels = None
depends_on = None

APPEND_ONLY = (
    "ledger_entries",
    "ledger_transactions",
    "usage_events",
    "subscription_history",
    "admin_audit",
)

FORBID_FN = """CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql SET search_path = billing, pg_temp AS $fn$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$fn$"""

BALANCED_FN = """CREATE FUNCTION assert_ledger_balanced() RETURNS trigger LANGUAGE plpgsql SET search_path = billing, pg_temp AS $fn$
BEGIN
  IF (SELECT sum(amount_paise) FROM ledger_entries
      WHERE transaction_id = NEW.transaction_id) <> 0 THEN
    RAISE EXCEPTION 'ledger transaction % does not balance', NEW.transaction_id;
  END IF;
  RETURN NULL;
END
$fn$"""

BALANCED_TRIGGER = """CREATE CONSTRAINT TRIGGER ledger_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_ledger_balanced()"""

INSERT_ONLY = ", ".join(APPEND_ONLY)
READ_ONLY = ("plan_versions",)
SELECT_INSERT = ("model_prices", "fx_rates")
SELECT_INSERT_UPDATE = (
    "billing_accounts",
    "subscriptions",
    "plan_overrides",
    "payments",
    "refunds",
    "invoices",
    "invoice_lines",
    "invoice_counters",
    "ledger_accounts",
    "ledger_balances",
    "credit_lots",
    "webhook_inbox",
    "usage_daily",
)
FULL = ("processed_events", "outbox", "idempotency_keys", "jobs")


def upgrade() -> None:
    op.execute("SET search_path = billing")
    op.execute(FORBID_FN)
    for t in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION forbid_change()"
        )
    op.execute(BALANCED_FN)
    op.execute(BALANCED_TRIGGER)

    op.execute("GRANT USAGE ON SCHEMA billing TO billing_app")
    op.execute("GRANT SELECT, INSERT ON " + INSERT_ONLY + " TO billing_app")
    op.execute("REVOKE UPDATE, DELETE ON " + INSERT_ONLY + " FROM billing_app")
    op.execute("GRANT SELECT ON " + ", ".join(READ_ONLY) + " TO billing_app")
    op.execute("GRANT SELECT, INSERT ON " + ", ".join(SELECT_INSERT) + " TO billing_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON " + ", ".join(SELECT_INSERT_UPDATE) + " TO billing_app"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON " + ", ".join(FULL) + " TO billing_app")
    op.execute("GRANT SELECT ON alembic_version TO billing_app")  # /readyz checks the head
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA billing TO billing_app")
    op.execute("GRANT EXECUTE ON FUNCTION billing.ensure_usage_partitions(int) TO billing_app")
    # Partitions that exist now, and ones the SECURITY DEFINER function creates later.
    op.execute(
        """DO $do$
DECLARE p regclass;
BEGIN
  FOR p IN SELECT inhrelid::regclass FROM pg_inherits
           WHERE inhparent = 'billing.usage_events'::regclass LOOP
    EXECUTE format('GRANT SELECT, INSERT ON %s TO billing_app', p);
    EXECUTE format('REVOKE UPDATE, DELETE ON %s FROM billing_app', p);
  END LOOP;
END
$do$"""
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA billing GRANT SELECT, INSERT ON TABLES TO billing_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA billing GRANT USAGE, SELECT ON SEQUENCES "
        "TO billing_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA billing REVOKE SELECT, INSERT ON TABLES FROM billing_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA billing REVOKE USAGE, SELECT ON SEQUENCES "
        "FROM billing_app"
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA billing FROM billing_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA billing FROM billing_app")
    op.execute("REVOKE EXECUTE ON FUNCTION billing.ensure_usage_partitions(int) FROM billing_app")
    op.execute("REVOKE USAGE ON SCHEMA billing FROM billing_app")
    op.execute("DROP TRIGGER IF EXISTS ledger_balanced ON billing.ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS billing.assert_ledger_balanced()")
    for t in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON billing.{t}")
    op.execute("DROP FUNCTION IF EXISTS billing.forbid_change()")
