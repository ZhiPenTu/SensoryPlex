#!/usr/bin/env bash
# 停止并移除本地 POC 容器栈；默认保留命名卷（postgres-data / nats-data）。
# - `--volumes` 会连数据一起删除，请谨慎传参。
# - 调用：`./deploy/down.sh [--volumes] [额外 docker compose 参数]`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[down] .env 不存在，请确认仓库根目录" >&2
    exit 1
fi

cd "${ROOT}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" down "$@"
echo "[down] 已停止；如需清理数据，请使用 ./deploy/down.sh --volumes"
