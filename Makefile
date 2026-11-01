export BILLING_MIGRATION_DATABASE_URL ?= postgresql+asyncpg://billing_migrator:billing_migrator@localhost:5433/billing_db

.PHONY: sync up down db-create test lint fmt migrate catalog-sync run-api

sync:
	uv sync

# Shared Postgres/Kafka: start with `make -C .. infra` (../docker-compose.yml). This adds billing's Valkey.
up:
	docker compose up -d --remove-orphans
	docker exec -i graphgpt-postgres psql -U postgres < deploy/sql/roles.sql

db-create:
	docker exec -i graphgpt-postgres psql -U postgres < deploy/sql/roles.sql

down:
	docker compose down

lint:
	uv run ruff check src tests
	uv run mypy
	uv run lint-imports

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

test:
	uv run pytest --cov=billing.domain --cov-fail-under=95

migrate:
	uv run alembic upgrade head

catalog-sync:
	@echo "catalog sync arrives in Phase 3"

run-api:
	uv run uvicorn billing.main_api:app --port 8080 --reload
