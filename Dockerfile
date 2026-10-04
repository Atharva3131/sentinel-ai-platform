# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.7.13 AS uv

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 sentinel \
    && useradd --system --uid 10001 --gid sentinel --home-dir /app sentinel

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --chown=sentinel:sentinel pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=sentinel:sentinel backend ./backend
COPY --chown=sentinel:sentinel main.py alembic.ini ./
RUN uv sync --frozen --no-dev

USER sentinel

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
