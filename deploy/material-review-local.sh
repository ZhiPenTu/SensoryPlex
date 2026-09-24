#!/bin/sh
# 独立验收栈只消费本机配置，不默认创建样例或操作主业务库。
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -f .data/material-review/preview.env ] || [ ! -f .data/material-review/compose.yml ]; then
    echo '缺少独立验收配置，请参照 docs/runbooks/material-review.md；不会启动主业务栈。' >&2
    exit 1
fi
exec docker compose --env-file .data/material-review/preview.env -f .data/material-review/compose.yml "$@"
