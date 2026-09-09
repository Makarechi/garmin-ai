FROM ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 AS uv
FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
CMD ["garmin-ai", "worker"]
