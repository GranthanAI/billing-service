.PHONY: sync up down test lint fmt migrate catalog-sync run-api

sync:
	uv sync

up:
	docker compose up -d

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
