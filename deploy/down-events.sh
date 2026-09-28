#!/usr/bin/env bash
# 停止并移除事件链路常驻服务（relay / index / VLM publisher / VLM result fuser）；默认保留向量库文件 .data/index/。
# - 基础栈（postgres / nats / api / console / gateway）不受影响。
# - `--volumes` 会连向量库一起删除，请谨慎传参。
# - 调用：`./deploy/down-events.sh [--volumes]`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"
INDEX_DIR="${ROOT}/.data/index"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[events-down] .env 不存在，请确认仓库根目录" >&2
    exit 1
fi

remove_volumes=0
for argument in "$@"; do
    if [[ "${argument}" == "--volumes" ]]; then
        remove_volumes=1
    fi
done

cd "${ROOT}"
# 只移除这两个服务：`docker compose down` 会把整个项目（含 api/postgres）都拆掉。
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile events rm -sf \
    relay index vlm-publisher vlm-result-fuser

if [[ "${remove_volumes}" -eq 1 ]]; then
    rm -rf "${INDEX_DIR}"
    echo "[events-down] 已删除向量库目录 ${INDEX_DIR}"
fi

echo "[events-down] 事件链路已停止；如需连向量库一起清理，请使用 ./deploy/down-events.sh --volumes"
