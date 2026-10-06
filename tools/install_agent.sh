#!/usr/bin/env bash
# Compatibility shim forwarding to tools/workers/install_agent.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/workers/install_agent.sh" "$@"
