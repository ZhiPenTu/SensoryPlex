FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY plugins/python/common/pyproject.toml plugins/python/common/pyproject.toml
COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
RUN --mount=type=cache,id=sensoryplex-uv,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace
COPY plugins/python/common plugins/python/common
COPY services/gateway services/gateway
RUN --mount=type=cache,id=sensoryplex-uv,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
COPY tools/migrate.py tools/migrate.py
COPY db/migrations db/migrations
RUN groupadd --gid 10001 sensoryplex && useradd --uid 10001 --gid 10001 --no-create-home sensoryplex
USER 10001:10001
EXPOSE 8090
CMD ["/app/.venv/bin/uvicorn", "sensoryplex_gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
