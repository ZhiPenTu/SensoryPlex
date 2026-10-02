#!/usr/bin/env bash
# 启动事件链路常驻服务：素材 relay/index，以及 VLM 慢路径 publisher/result-fuser。
# - 这两个服务在 compose 里属于 `events` profile，`./deploy/up.sh` 默认不拉起它们。
# - 依赖基础栈（postgres / nats / migrate）与 api 镜像；本脚本会自行等到健康为止。
# - 前置条件：BGE 权重已固化到 .data/models/bge-small-zh-v1.5（见下面缺失时的提示）。
# - 调用：`./deploy/up-events.sh [额外 docker compose 参数]`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
COMPOSE_FILE="${ROOT}/deploy/compose/docker-compose.poc.yml"
# 索引进程要求显式给出权重目录；这里与 compose 的 --model-dir 保持同一个路径（相对仓库根）。
MODEL_DIR="${ROOT}/.data/models/bge-small-zh-v1.5"
MODEL_FILE="${MODEL_DIR}/onnx/model_quantized.onnx"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[events-up] .env 不存在，请先执行 make configure 生成密钥" >&2
    exit 1
fi

if [[ ! -f "${MODEL_FILE}" ]]; then
    echo "[events-up] 缺少 BGE 权重：${MODEL_FILE}" >&2
    echo "[events-up] 从本机 HF 缓存固化（-L 解引用软链，否则容器里读不到 blob）：" >&2
    echo "[events-up]   mkdir -p .data/models && cp -RL \"\$HOME/.cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/snapshots/75c43b069aac4d136ba6bc1122f995fedcfd2781\" .data/models/bge-small-zh-v1.5" >&2
    echo "[events-up] 权重不进仓库（.data/ 已被 .gitignore 排除）；缺失时索引进程会显式失败，不会联网下载。" >&2
    exit 1
fi

cd "${ROOT}"
# 两个落盘位置都在仓库 bind mount 下（主机与容器同视角）：状态行要给验收读，
# 向量库文件要由非 root 的容器用户创建——命名卷的挂载点是 root 所有，写不进去。
mkdir -p "${ROOT}/.data/events" "${ROOT}/.data/index"
# 不传 --build：镜像缺失时 compose 会自己构建（这两个服务与 api 共用同一份 Dockerfile 与镜像标签）。
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile events up -d --wait \
    relay index vlm-publisher vlm-result-fuser enrichment-publisher enrichment-result-fuser "$@"

echo "[events-up] 事件链路状态："
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile events ps \
    relay index vlm-publisher vlm-result-fuser enrichment-publisher enrichment-result-fuser

echo
echo "[events-up] 常驻进程的落盘位置（仓库 bind mount，主机与容器同视角）："
echo "  relay 状态行   .data/events/relay-status.json"
echo "  index 状态行   .data/events/index-status.json"
echo "  index 就绪行   .data/events/index-ready.json"
echo "  VLM 发布状态   .data/events/vlm-publisher-status.json"
echo "  VLM 融合状态   .data/events/vlm-result-fuser-status.json"
echo "  VLM WorkQueue  sensoryplex-tasks（publisher + result-fuser 在 compose 内常驻）"
echo
echo "[events-up] 检索面只在 compose 网络内暴露（index:50077，令牌见 .env 的 SENSORYPLEX_INDEX_AUTH_TOKEN）；"
echo "            完整链路的验收在容器内执行：make event-pipeline-check"
