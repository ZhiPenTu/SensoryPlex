"""Tests for tools/setup_mcp.py configuration script."""

import json
import sys
from pathlib import Path

# Add repo root to sys.path to import tools.setup_mcp
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tools.setup_mcp import (  # noqa: E402
    build_mcp_config,
    configure_claude_desktop,
    install_codex_skill,
)


def test_build_mcp_config():
    config = build_mcp_config("http://127.0.0.1:8091", "token123")
    assert config["command"] == "uv"
    assert "sensoryplex-mcp" in config["args"]
    assert config["env"]["SENSORYPLEX_BASE_URL"] == "http://127.0.0.1:8091"
    assert config["env"]["SENSORYPLEX_API_TOKEN"] == "token123"


def test_configure_claude_desktop(tmp_path):
    config_file = tmp_path / "claude_desktop_config.json"

    # Pre-populate with existing server
    initial = {
        "mcpServers": {
            "existing-server": {
                "command": "node",
                "args": ["server.js"],
            }
        }
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    mcp_entry = build_mcp_config("http://localhost:8091", "mytoken")
    ok = configure_claude_desktop(config_file, mcp_entry)
    assert ok is True

    result = json.loads(config_file.read_text(encoding="utf-8"))
    assert "existing-server" in result["mcpServers"]
    assert "sensoryplex" in result["mcpServers"]
    assert result["mcpServers"]["sensoryplex"]["env"]["SENSORYPLEX_API_TOKEN"] == "mytoken"


def test_install_codex_skill(tmp_path):
    codex_home = tmp_path / ".codex"
    ok = install_codex_skill(codex_home)
    assert ok is True

    target_skill_file = codex_home / "skills/sensoryplex/SKILL.md"
    assert target_skill_file.is_file()
    content = target_skill_file.read_text(encoding="utf-8")
    assert "name: sensoryplex" in content
    assert "SensoryPlex Multimodal Video Intelligence Skill" in content
