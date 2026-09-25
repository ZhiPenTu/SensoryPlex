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

echo
echo "[up] 自动纳管并拉起本机同机计算节点 (ADR-026)..."
# 按 AGENTS.md：底座相关验证一律容器内执行。node_agent.py 依赖 edge_material_sdk，
# 主机 Python 不再维护底座 SDK，因此同机自纳管必须跑在 api 容器里，由容器内
# 已就绪的 uv venv + env 中的 SENSORYPLEX_API_TOKEN 完成。
# 这是"把本机声明为同机数据面节点"的一次性注册；节点身份由主节点 Registry 持有，
# 不在主机起常驻 daemon。需要 daemon 化请独立在主机执行
#   ./tools/install_agent.sh --local --install-service
# （那一步会引导用户在桌面主机上把 Node Agent 注册为 launchd 服务）。
HOSTNAME_LABEL="$(hostname -s 2>/dev/null || uname -n || echo local)"
if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
        ps --services --status running 2>/dev/null | grep -qx "api"; then
    echo "[up] api 容器未运行，跳过本机同机节点自纳管" >&2
elif docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
        exec -T api /app/.venv/bin/python /workspace/tools/node_agent.py enroll \
            --main-url "http://127.0.0.1:${API_PORT}" \
            --node-id "local-host" \
            --display-name "本机数据面 (${HOSTNAME_LABEL})" \
            --co-located \
            --local \
            --state-file /workspace/.data/agent/local-host.json; then
    echo "[up] 本机同机节点 local-host 已纳管 (co-located, ADR-026)。"
else
    echo "[up] 本机同机节点自纳管失败，可手动在 api 容器内重试或独立在主机执行 tools/install_agent.sh --local" >&2
fi
