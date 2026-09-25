#!/usr/bin/env bash
# 打印容器栈当前状态与关键端口/健康检查结果。
# - 先 `docker compose ps`，再独立探测 console/docs/api/gateway 的 HTTP 端点。
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
DOCS_PORT="${DOCS_PORT:-5174}"
API_PORT="${API_PORT:-8091}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"

# 运行中的服务名单：用来把"这个服务不在当前栈里"与"服务起了但探不通"区分开。
# 文档站是纯静态的附加服务，旧栈里可能没有它；那时 SKIP 比 FAIL 更诚实——
# 探一个根本没被拉起的端口，只会把 status 输出变成噪声。
running_services="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
    ps --services --status running 2>/dev/null || true)"
is_running() { grep -qx "$1" <<<"${running_services}"; }

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
if is_running docs; then
    probe "docs   " "http://127.0.0.1:${DOCS_PORT}/"
else
    echo "[status] SKIP docs   未运行（./deploy/up.sh docs 可单独启动）"
fi
probe "api    " "http://127.0.0.1:${API_PORT}/livez"
probe "api    " "http://127.0.0.1:${API_PORT}/v1/health"
probe "gateway" "http://127.0.0.1:${GATEWAY_PORT}/v1/health"
