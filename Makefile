export BILLING_MIGRATION_DATABASE_URL ?= postgresql+asyncpg://billing_migrator:billing_migrator@localhost:5433/billing_db

.PHONY: topics proto sync up down db-create test lint fmt migrate catalog-sync run-api

sync:
	uv sync

# Shared Postgres/Kafka: start with `make -C .. infra` (../docker-compose.yml). This adds billing's Valkey.
up:
	docker compose up -d --remove-orphans
	docker exec -i graphgpt-postgres psql -U postgres < deploy/sql/roles.sql
	-make topics

topics:
	@for t in $$(uv run python -m billing.app.topics); do \
		MSYS_NO_PATHCONV=1 docker exec graphgpt-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
			--create --if-not-exists --topic $$t --partitions 6 --replication-factor 1; \
	done

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
	uv run python -m billing.cli pricing seed
	PAYMENT_GATEWAY=fake uv run python -m billing.cli catalog sync

run-api:
	uv run uvicorn billing.main_api:app --port 8080 --reload

proto:
	uv run python scripts/gen_proto.py
