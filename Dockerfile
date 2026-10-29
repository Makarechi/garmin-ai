FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.15.2-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd AS builder
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --locked --no-dev --no-install-project --extra full
COPY src ./src
COPY alembic.ini ./
RUN uv sync --locked --no-dev --no-editable --extra full
COPY scripts/collect_python_notices.py ./scripts/
RUN .venv/bin/python scripts/collect_python_notices.py \
    --notices /app/python-third-party-notices.txt \
    --inventory /app/python-third-party-inventory.json

FROM python:3.15.2-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY alembic.ini ./
COPY LICENSE /usr/share/doc/garmin-ai/LICENSE
COPY --from=builder /app/python-third-party-notices.txt /usr/share/doc/garmin-ai/python-third-party-notices.txt
COPY --from=builder /app/python-third-party-inventory.json /usr/share/doc/garmin-ai/python-third-party-inventory.json
ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
CMD ["garmin-ai", "worker"]
