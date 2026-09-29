#!/usr/bin/env bash
# 为已验证的原生插件安装单并发队列工作器。配置和制品路径只属于宿主运维输入。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != Darwin || $# != 2 ]]; then
  echo "用法（macOS）: ./deploy/up-vlm-worker.sh <已安装 release 目录> <配置 JSON>" >&2
  exit 2
fi
RELEASE="$(cd "$1" && pwd)"
CONFIG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
test -x "${RELEASE}/venv/bin/python"
test -f "${RELEASE}/INSTALLED.json"
test -f "$CONFIG"
mkdir -p "${ROOT}/.data/vlm-worker"
PLIST="${HOME}/Library/LaunchAgents/org.sensoryplex.vlm-worker.local-host.plist"
"${RELEASE}/venv/bin/python" - "$ROOT" "$RELEASE" "$CONFIG" "$PLIST" <<'PY'
import plistlib
import sys
from pathlib import Path
root, release, config, destination = map(Path, sys.argv[1:])
service = {
    "Label": "org.sensoryplex.vlm-worker.local-host",
    "ProgramArguments": [str(release / "venv/bin/python"), str(root / "tools/vlm_task_worker.py"),
        "--nats-url", "nats://127.0.0.1:24222", "--media-root", str(root / ".data/console-media"),
        "--config", str(config)],
    "EnvironmentVariables": {"PYTHONPATH": str(release / "payload/src"),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
    "WorkingDirectory": str(root), "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
    "StandardOutPath": str(root / ".data/vlm-worker/worker.log"),
    "StandardErrorPath": str(root / ".data/vlm-worker/worker.err.log"),
}
destination.write_bytes(plistlib.dumps(service))
destination.chmod(0o600)
PY
LABEL="gui/$(id -u)/org.sensoryplex.vlm-worker.local-host"
if launchctl print "$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$LABEL"
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "VLM 单并发工作器已启动；结果日志：.data/vlm-worker/worker.log"
