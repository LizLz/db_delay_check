# One image for the web app AND the daily updater (same code, different command).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# dependencies first: this layer is cached until pyproject.toml / uv.lock change
COPY pyproject.toml uv.lock* README* ./
COPY src ./src
RUN uv sync --no-dev --extra download

COPY web ./web
COPY scripts ./scripts
COPY fixtures ./fixtures

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/delays.duckdb \
    REPORT_CACHE=/app/data/report_cache.json

EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
