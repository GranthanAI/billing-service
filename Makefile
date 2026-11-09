export BILLING_MIGRATION_DATABASE_URL ?= postgresql+asyncpg://billing_migrator:billing_migrator@localhost:5433/billing_db

.PHONY: test-fast test-int test-file test-full-bg test-up test-down test-integration test-all audit alerts-check proto-check loadtest topics proto sync up down db-create test lint fmt migrate catalog-sync run-api

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

# --- tests: tiers (see README "Testing") ---------------------------------------------------------
# The integration tiers reuse the services of docker-compose.test.yml (non-default ports), each
# xdist worker with its own databases and Valkey db index. Without `make test-up` they would fall
# back to Testcontainers; the targets below start the services themselves.
TEST_ENV = TEST_PG_URL=postgresql://postgres:postgres@localhost:55432/postgres \
	TEST_VALKEY_URL=redis://localhost:56379 TEST_VALKEY_LRU_URL=redis://localhost:56380/0 \
	TEST_KAFKA_BOOTSTRAP=localhost:59092
PYTEST = uv run pytest -p no:cacheprovider
# Docker Desktop on Windows drops connections above ~3 workers; TEST_WORKERS overrides.
WORKERS = $(or $(TEST_WORKERS),$(if $(findstring Windows,$(OS)),3,4))

test-up:
	docker compose -f docker-compose.test.yml up -d --wait

test-down:
	docker compose -f docker-compose.test.yml down -v

# Run after every change: unit + property + health, no coverage, stops at the first failure.
test-fast:
	HYPOTHESIS_PROFILE=fast $(PYTEST) tests/unit tests/property tests/test_health.py -q -x -n 2 -m "not slow"

# Before each commit: the integration tests that are not marked slow.
test-int: test-up
	$(TEST_ENV) $(PYTEST) tests/integration -n $(WORKERS) --dist loadgroup -m "not slow" -q

# One file against the running services: make test-file FILE=tests/integration/test_x.py
test-file: test-up
	$(TEST_ENV) $(PYTEST) $(FILE) -x -q

# Once per phase, at the end: everything, with the 95% coverage gate on billing.domain.
test-all: test-up
	$(TEST_ENV) $(PYTEST) tests -n $(WORKERS) --dist loadgroup --cov=billing.domain --cov-fail-under=95 \
		--durations=20 -q -rfE

# test-all in the background; returns at once. Read reports/test-run-latest.txt when it is done.
test-full-bg:
	bash scripts/test_full_bg.sh

# Old names, kept: the slow serial runs on Testcontainers.
test:
	uv run pytest tests/unit tests/property tests/test_health.py --cov=billing.domain --cov-fail-under=95

test-integration:
	uv run pytest tests/integration

migrate:
	uv run alembic upgrade head

catalog-sync:
	uv run python -m billing.cli pricing seed
	PAYMENT_GATEWAY=fake uv run python -m billing.cli catalog sync

run-api:
	uv run uvicorn billing.main_api:app --port 8080 --reload

proto:
	uv run python scripts/gen_proto.py

# buf lint, and buf breaking against main. billing.proto is the only proto left: Kafka events
# are JSON, so there are no event protos to compare.
# Uses a local `buf` when there is one, otherwise the official image through Docker.
BUF = $(if $(shell which buf 2>/dev/null),buf,MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR):/w" -w /w bufbuild/buf)
proto-check:
	$(BUF) lint proto
	$(BUF) breaking proto --against '.git#branch=main,subdir=proto'

loadtest:
	uv run python loadtest/run.py

# Dependency vulnerabilities (needs network access to the advisory database).
audit:
	uv run pip-audit

# Alert rules: syntax, then the unit tests in deploy/prometheus/billing-alerts.test.yml.
# Uses a local promtool when there is one, otherwise the Prometheus image through Docker.
PROMTOOL = $(if $(shell which promtool 2>/dev/null),promtool,MSYS_NO_PATHCONV=1 docker run --rm --entrypoint promtool -v "$(CURDIR)/deploy/prometheus:/rules" -w /rules prom/prometheus)
alerts-check:
	$(PROMTOOL) check rules billing-alerts.yml
	$(PROMTOOL) test rules billing-alerts.test.yml
