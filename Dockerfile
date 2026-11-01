FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
# api:    uvicorn billing.main_api:app --host 0.0.0.0 --port 8080
# worker: python -m billing.main_worker
CMD ["uvicorn", "billing.main_api:app", "--host", "0.0.0.0", "--port", "8080"]
