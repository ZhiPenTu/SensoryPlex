#!/usr/bin/env bash
# SensoryPlex Node Agent 一键安装与服务管理脚本 (ADR-026)
# - 支持同机自纳管：./tools/install_agent.sh --local [--daemon]
# - 支持远端一键入网：curl -fsSL http://<ip>:8091/v1/agent/install.sh | bash -s -- --token <token> [--daemon]
set -euo pipefail

MAIN_URL="${SENSORYPLEX_MAIN_URL:-http://127.0.0.1:8091}"
TOKEN=""
IS_LOCAL=0
IS_CANDIDATE=0
NODE_ID=""
DISPLAY_NAME=""
DAEMON=0
INSTALL_SERVICE=0
ACTION="install"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)
            IS_LOCAL=1
            shift
            ;;
        --candidate)
            IS_CANDIDATE=1
            shift
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --main-url)
            MAIN_URL="$2"
            shift 2
            ;;
        --node-id)
            NODE_ID="$2"
            shift 2
            ;;
        --display-name)
            DISPLAY_NAME="$2"
            shift 2
            ;;
        --daemon)
            DAEMON=1
            shift
            ;;
        --install-service)
            INSTALL_SERVICE=1
            DAEMON=1
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --uninstall)
            ACTION="uninstall"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        -h|--help)
            echo "SensoryPlex Node Agent Installer"
            echo "用法:"
            echo "  同机快速自纳管:       ./tools/install_agent.sh --local [--daemon]"
            echo "  远程节点一键入网:     ./tools/install_agent.sh --token <TOKEN> --main-url <URL> [--daemon]"
            echo "  停止后台 Agent:       ./tools/install_agent.sh --stop [--node-id <NODE_ID>]"
            echo "  干净卸载 Agent:       ./tools/install_agent.sh --uninstall [--node-id <NODE_ID>]"
            echo "  检查 Agent 状态:      ./tools/install_agent.sh --status [--node-id <NODE_ID>]"
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

# 确定工作目录与 Python
if [[ -d "tools" && -f "tools/node_agent.py" ]]; then
    AGENT_DIR="$(pwd)"
    STATE_DIR="${AGENT_DIR}/.data/agent"
    AGENT_SCRIPT="${AGENT_DIR}/tools/node_agent.py"
else
    AGENT_DIR="${HOME}/.sensoryplex/agent"
    STATE_DIR="${AGENT_DIR}/data"
    AGENT_SCRIPT="${AGENT_DIR}/node_agent.py"
    mkdir -p "${AGENT_DIR}"
fi
mkdir -p "${STATE_DIR}"

# 寻找 python3
PYTHON_BIN=""
if [[ -x "${AGENT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${AGENT_DIR}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "[agent-installer] 错误: 系统未检测到 python3，请先安装 Python 3.10+" >&2
    exit 1
fi

# 默认 node_id 与 display_name
HOSTNAME_LABEL="$(hostname -s 2>/dev/null || uname -n)"
HOSTNAME_SAFE="$(echo "${HOSTNAME_LABEL}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-')"

if [[ -z "${NODE_ID}" ]]; then
    if [[ ${IS_LOCAL} -eq 1 ]]; then
    if [[ -z "${SENSORYPLEX_API_TOKEN:-}" && -f "${AGENT_DIR}/.env" ]]; then
        export SENSORYPLEX_API_TOKEN="$(grep -E "^SENSORYPLEX_API_TOKEN=" "${AGENT_DIR}/.env" | cut -d= -f2-)"
    fi
        NODE_ID="local-host"
        DISPLAY_NAME="${DISPLAY_NAME:-本机数据面 (${HOSTNAME_LABEL})}"
    else
        NODE_ID="node-${HOSTNAME_SAFE}"
        DISPLAY_NAME="${DISPLAY_NAME:-节点 ${HOSTNAME_LABEL}}"
    fi
fi
if [[ -z "${DISPLAY_NAME}" ]]; then
    DISPLAY_NAME="节点 ${NODE_ID}"
fi

STATE_FILE="${STATE_DIR}/${NODE_ID}.json"
PID_FILE="${STATE_DIR}/${NODE_ID}.pid"
LOG_FILE="${STATE_DIR}/${NODE_ID}.log"

# 停止服务动作
if [[ "${ACTION}" == "stop" ]]; then
    if [[ "$(uname -s)" == "Darwin" && -f "${HOME}/Library/LaunchAgents/org.sensoryplex.agent.${NODE_ID}.plist" ]]; then
        launchctl unload "${HOME}/Library/LaunchAgents/org.sensoryplex.agent.${NODE_ID}.plist" 2>/dev/null || true
        rm -f "${HOME}/Library/LaunchAgents/org.sensoryplex.agent.${NODE_ID}.plist"
    fi
    PIDS="$(pgrep -f "node_agent.py run --node-id ${NODE_ID}" || true)"
    if [[ -n "${PIDS}" ]]; then
        echo "[agent-installer] 正在停止 Agent 进程 (${PIDS})..."
        kill ${PIDS} 2>/dev/null || true
        sleep 0.5
        kill -9 ${PIDS} 2>/dev/null || true
        echo "[agent-installer] Agent 已停止"
    else
        echo "[agent-installer] Agent 未在运行 (node_id: ${NODE_ID})"
    fi
    rm -f "${PID_FILE}"
    exit 0
fi

# 干净卸载动作
if [[ "${ACTION}" == "uninstall" ]]; then
    echo "======================================================================"
    echo " SensoryPlex Node Agent 干净卸载 (Clean Uninstall)"
    echo " 节点标识: ${NODE_ID}"
    echo "======================================================================"

    # 1. 停止系统常驻服务并删除 plist / systemd 配置
    if [[ "$(uname -s)" == "Darwin" ]]; then
        PLIST_FILE="${HOME}/Library/LaunchAgents/org.sensoryplex.agent.${NODE_ID}.plist"
        if [[ -f "${PLIST_FILE}" ]]; then
            echo "[agent-installer] 正在卸载 macOS launchd 守护服务..."
            launchctl unload "${PLIST_FILE}" 2>/dev/null || true
            rm -f "${PLIST_FILE}"
        fi
    elif [[ -f "/etc/systemd/system/sensoryplex-agent-${NODE_ID}.service" ]]; then
        echo "[agent-installer] 正在卸载 Linux systemd 守护服务..."
        systemctl stop "sensoryplex-agent-${NODE_ID}" 2>/dev/null || true
        systemctl disable "sensoryplex-agent-${NODE_ID}" 2>/dev/null || true
        rm -f "/etc/systemd/system/sensoryplex-agent-${NODE_ID}.service"
    fi

    # 2. 终止本地 Agent 进程
    PIDS="$(pgrep -f "node_agent.py run --node-id ${NODE_ID}" || true)"
    if [[ -n "${PIDS}" ]]; then
        echo "[agent-installer] 正在终止 Agent 进程 (${PIDS})..."
        kill ${PIDS} 2>/dev/null || true
        sleep 0.5
        kill -9 ${PIDS} 2>/dev/null || true
    fi

    # 3. 驱动 Python 反注册并清理该节点所有插件实例目录
    if [[ -f "${AGENT_SCRIPT}" ]]; then
        echo "[agent-installer] 正在向主节点注销并清理本地插件环境..."
        "${PYTHON_BIN}" "${AGENT_SCRIPT}" deregister \
            --node-id "${NODE_ID}" \
            --state-file "${STATE_FILE}" \
            --main-url "${MAIN_URL}" || true
    fi

    # 4. 清理本地 PID、状态与日志文件
    rm -f "${PID_FILE}" "${STATE_FILE}" "${LOG_FILE}"
    echo "[agent-installer] ✅ 节点 ${NODE_ID} 已彻底干净卸载（系统服务已删除、进程已终止、所有插件与本地状态已清除）"
    exit 0
fi

# 查询状态动作
if [[ "${ACTION}" == "status" ]]; then
    PIDS="$(pgrep -f "node_agent.py run --node-id ${NODE_ID}" || true)"
    if [[ -n "${PIDS}" ]]; then
        echo "[agent-installer] Agent 运行中 (node_id: ${NODE_ID}, PID: ${PIDS})"
        exit 0
    fi
    echo "[agent-installer] Agent 未在运行 (node_id: ${NODE_ID})"
    exit 1
fi

# 下载最新的 agent 脚本（如果是远程 curl 安装环境）
if [[ ! -f "${AGENT_SCRIPT}" ]]; then
    echo "[agent-installer] 正在从主节点获取 node_agent.py..."
    curl -fsSL "${MAIN_URL}/v1/agent/node_agent.py" -o "${AGENT_SCRIPT}"
fi

# 步骤 1：入网注册 (Enroll / Bootstrap)
echo "======================================================================"
echo " SensoryPlex Node Agent 一键安装"
echo " 目标主节点: ${MAIN_URL}"
echo " 节点标识:   ${NODE_ID} (${DISPLAY_NAME})"
echo "======================================================================"

if [[ ${IS_LOCAL} -eq 1 ]]; then
    if [[ -z "${SENSORYPLEX_API_TOKEN:-}" && -f "${AGENT_DIR}/.env" ]]; then
        export SENSORYPLEX_API_TOKEN="$(grep -E "^SENSORYPLEX_API_TOKEN=" "${AGENT_DIR}/.env" | cut -d= -f2-)"
    fi
    echo "[agent-installer] 执行同机数据面自纳管 (Bootstrap Local)..."
    "${PYTHON_BIN}" "${AGENT_SCRIPT}" enroll \
        --main-url "${MAIN_URL}" \
        --node-id "${NODE_ID}" \
        --display-name "${DISPLAY_NAME}" \
        --co-located \
        --local \
        --state-file "${STATE_FILE}"
elif [[ ${IS_CANDIDATE} -eq 1 ]]; then
    echo "[agent-installer] 作为候选节点自报到 (Candidate Register)..."
    "${PYTHON_BIN}" "${AGENT_SCRIPT}" enroll \
        --main-url "${MAIN_URL}" \
        --node-id "${NODE_ID}" \
        --display-name "${DISPLAY_NAME}" \
        --candidate \
        --state-file "${STATE_FILE}"
else
    if [[ -z "${TOKEN}" ]]; then
        echo "[agent-installer] 错误: 远程节点入网必须提供 --token <TOKEN>" >&2
        exit 1
    fi
    echo "[agent-installer] 正在使用注册令牌入网..."
    "${PYTHON_BIN}" "${AGENT_SCRIPT}" enroll \
        --main-url "${MAIN_URL}" \
        --node-id "${NODE_ID}" \
        --display-name "${DISPLAY_NAME}" \
        --token "${TOKEN}" \
        --state-file "${STATE_FILE}"
fi

# 步骤 2：启动服务 (Daemon / Foreground)
if [[ ${DAEMON} -eq 1 ]]; then
    # 先停止旧的后台进程
    if [[ -f "${PID_FILE}" ]]; then
        OLD_PID="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
        if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
            kill -9 "${OLD_PID}" 2>/dev/null || true
        fi
        rm -f "${PID_FILE}"
    fi

    # macOS launchd 服务化
    if [[ ${INSTALL_SERVICE} -eq 1 && "$(uname -s)" == "Darwin" ]]; then
        PLIST_DIR="${HOME}/Library/LaunchAgents"
        PLIST_FILE="${PLIST_DIR}/org.sensoryplex.agent.${NODE_ID}.plist"
        mkdir -p "${PLIST_DIR}"
        cat << PLIST_EOF > "${PLIST_FILE}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.sensoryplex.agent.${NODE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v "${PYTHON_BIN}")</string>
        <string>${AGENT_SCRIPT}</string>
        <string>run</string>
        <string>--node-id</string>
        <string>${NODE_ID}</string>
        <string>--state-file</string>
        <string>${STATE_FILE}</string>
        <string>--main-url</string>
        <string>${MAIN_URL}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF
        launchctl unload "${PLIST_FILE}" 2>/dev/null || true
        launchctl load "${PLIST_FILE}"
        echo "[agent-installer] 已安装为 macOS launchd 守护服务: ${PLIST_FILE}"
        echo "[agent-installer] 日志查看: tail -f ${LOG_FILE}"
        exit 0
    fi

    # 常规后台 nohup 守护进程
    PIDS="$(pgrep -f "node_agent.py run --node-id ${NODE_ID}" || true)"
    if [[ -n "${PIDS}" ]]; then
        echo "[agent-installer] Agent 已经在后台运行中 (PID: ${PIDS})"
        echo "${PIDS}" | head -n1 > "${PID_FILE}"
        exit 0
    fi

    echo "[agent-installer] 正在启动后台守护进程..."
    nohup "${PYTHON_BIN}" -u "${AGENT_SCRIPT}" run \
        --node-id "${NODE_ID}" \
        --state-file "${STATE_FILE}" \
        --main-url "${MAIN_URL}" < /dev/null >> "${LOG_FILE}" 2>&1 &
    
    AGENT_PID=$!
    disown "${AGENT_PID}" 2>/dev/null || true
    echo "${AGENT_PID}" > "${PID_FILE}"
    sleep 1

    if kill -0 "${AGENT_PID}" 2>/dev/null; then
        echo "[agent-installer] ✅ Agent 已在后台常驻运行 (PID: ${AGENT_PID})"
        echo "[agent-installer] 状态文件: ${STATE_FILE}"
        echo "[agent-installer] 运行日志: ${LOG_FILE}"
        echo "[agent-installer] 停止服务: ./tools/install_agent.sh --stop --node-id ${NODE_ID}"
    else
        echo "[agent-installer] ❌ Agent 启动失败，查看日志: cat ${LOG_FILE}" >&2
        cat "${LOG_FILE}" >&2
        exit 1
    fi
else
    echo "[agent-installer] 正在前台运行 Agent 心跳（按 Ctrl+C 退出）..."
    exec "${PYTHON_BIN}" "${AGENT_SCRIPT}" run \
        --node-id "${NODE_ID}" \
        --state-file "${STATE_FILE}" \
        --main-url "${MAIN_URL}"
fi
