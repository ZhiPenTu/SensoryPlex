#!/usr/bin/env bash
# 通用补全队列独立起停，不要求索引模型；仍使用基础栈的同一状态权威。
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${TASK_ROOT}"
test -f .env
docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml --profile events up -d \
    enrichment-publisher enrichment-result-fuser "$@"
