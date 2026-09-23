#!/usr/bin/env bash
# 启动本地 POC 容器栈：postgres + nats + migrate + gateway + api + console。
# - 端口、密钥由仓库根目录 .env 控制；若 .env 不存在请先运行 `make configure`。
# - 默认只在本机（127.0.0.1）暴露端口；不依赖外网访问。
# - 调用：`./deploy/up.sh [额外 docker compose 参数]`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[up] .env 不存在，请先执行 make configure 生成密钥" >&2
    exit 1
fi

cd "${ROOT}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --build --wait "$@"

echo "[up] 容器状态："
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps

CONSOLE_PORT="${CONSOLE_PORT:-5173}"
API_PORT="${API_PORT:-8091}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
POSTGRES_PORT="${POSTGRES_PORT:-25432}"
NATS_PORT="${NATS_PORT:-24222}"

echo
echo "[up] 本机访问入口："
echo "  前端 (console)    http://127.0.0.1:${CONSOLE_PORT}"
echo "  API   (api)       http://127.0.0.1:${API_PORT}"
echo "  网关 (gateway)    http://127.0.0.1:${GATEWAY_PORT}"
echo "  PostgreSQL        127.0.0.1:${POSTGRES_PORT}"
echo "  NATS              127.0.0.1:${NATS_PORT} / 监控 127.0.0.1:28222"
echo
echo "[up] 健康检查示例："
echo "  curl -fsS http://127.0.0.1:${API_PORT}/livez"
echo "  curl -fsS http://127.0.0.1:${API_PORT}/v1/health"
echo "  curl -fsS http://127.0.0.1:${GATEWAY_PORT}/v1/health"
