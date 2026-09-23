#!/usr/bin/env bash
# 跟踪单个或全部服务的实时日志。
# - 调用：`./deploy/logs.sh [服务名]`；不带参数等同于 `docker compose logs -f`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[logs] .env 不存在，请确认仓库根目录" >&2
    exit 1
fi

cd "${ROOT}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" logs -f --tail 200 "$@"
