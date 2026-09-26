#!/usr/bin/env bash
# 启动本地 POC 容器栈：postgres + nats + migrate + gateway + api + console + docs。
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
DOCS_PORT="${DOCS_PORT:-5174}"
API_PORT="${API_PORT:-8091}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
POSTGRES_PORT="${POSTGRES_PORT:-25432}"
NATS_PORT="${NATS_PORT:-24222}"

echo
echo "[up] 本机访问入口："
echo "  前端 (console)    http://127.0.0.1:${CONSOLE_PORT}"
echo "  文档 (docs)       http://127.0.0.1:${DOCS_PORT}"
echo "  API   (api)       http://127.0.0.1:${API_PORT}"
echo "  网关 (gateway)    http://127.0.0.1:${GATEWAY_PORT}"
echo "  PostgreSQL        127.0.0.1:${POSTGRES_PORT}"
echo "  NATS              127.0.0.1:${NATS_PORT} / 监控 127.0.0.1:28222"
echo
echo "[up] 健康检查示例："
echo "  curl -fsS http://127.0.0.1:${DOCS_PORT}/"
echo "  curl -fsS http://127.0.0.1:${API_PORT}/livez"
echo "  curl -fsS http://127.0.0.1:${API_PORT}/v1/health"
echo "  curl -fsS http://127.0.0.1:${GATEWAY_PORT}/v1/health"

echo
echo "[up] 检查本机同机计算节点登记状态 (ADR-026)..."
# Node Agent 是宿主原生插件执行器，能力必须由宿主机探测。若从 api 容器探测，
# 会把 macOS/Metal 主机误登记成 linux，继而破坏 release 平台选择与调度约束。
# 首次登记后 state file 保存会话；后续重建容器绝不能覆盖已登记的宿主节点身份。
# 常驻 Agent 由用户显式通过 tools/install_agent.sh 或 node_agent.py run 启动。
AGENT_STATE_FILE="${ROOT}/.data/agent/local-host.json"
HOSTNAME_LABEL="$(hostname -s 2>/dev/null || uname -n || echo local)"
if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
        ps --services --status running 2>/dev/null | grep -qx "api"; then
    echo "[up] api 容器未运行，跳过本机同机节点自纳管" >&2
elif [[ -f "${AGENT_STATE_FILE}" ]]; then
    echo "[up] 保留已登记的宿主节点 local-host；未从容器覆盖其能力。"
elif (
    set -a
    # .env 由 make configure 生成，向首次宿主登记提供 API bootstrap 凭据。
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
    uv run --frozen python tools/node_agent.py enroll \
        --main-url "http://127.0.0.1:${API_PORT}" \
        --node-id "local-host" \
        --display-name "本机数据面 (${HOSTNAME_LABEL})" \
        --co-located \
        --local \
        --state-file "${AGENT_STATE_FILE}"
); then
    echo "[up] 本机同机节点 local-host 已按宿主能力纳管 (co-located, ADR-026)。"
else
    echo "[up] 宿主节点首次纳管失败，可手动执行 tools/install_agent.sh --local" >&2
fi
