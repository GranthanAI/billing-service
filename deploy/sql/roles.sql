-- Creates the billing database and its two roles. Run once by a superuser; migrations never
-- create roles. Idempotent. Used by the shared compose init, `make db-create`, and the tests.
SELECT 'CREATE DATABASE billing_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'billing_db')\gexec

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'billing_migrator') THEN
    CREATE ROLE billing_migrator LOGIN PASSWORD 'billing_migrator';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'billing_app') THEN
    CREATE ROLE billing_app LOGIN PASSWORD 'billing_app';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE billing_db TO billing_migrator, billing_app;
GRANT CREATE ON DATABASE billing_db TO billing_migrator;
