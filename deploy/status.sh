#!/usr/bin/env bash
# 打印容器栈当前状态与关键端口/健康检查结果。
# - 先 `docker compose ps`，再独立探测 console/api/gateway 三个 HTTP 端点。
# - 调用：`./deploy/status.sh`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[status] .env 不存在，请确认仓库根目录" >&2
    exit 1
fi

cd "${ROOT}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps

CONSOLE_PORT="${CONSOLE_PORT:-5173}"
API_PORT="${API_PORT:-8091}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"

probe() {
    local label="$1" url="$2"
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "${url}" || echo "000")
    if [[ "${code}" =~ ^2|^3 ]]; then
        echo "[status] OK   ${label}  ${url}  -> ${code}"
    else
        echo "[status] FAIL ${label}  ${url}  -> ${code}"
    fi
}

probe "console" "http://127.0.0.1:${CONSOLE_PORT}/"
probe "api    " "http://127.0.0.1:${API_PORT}/livez"
probe "api    " "http://127.0.0.1:${API_PORT}/v1/health"
probe "gateway" "http://127.0.0.1:${GATEWAY_PORT}/v1/health"
