"""Tests for tools/setup_mcp.py configuration script."""

import json
import sys
from pathlib import Path

# Add repo root to sys.path to import tools.setup_mcp
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tools.setup_mcp import (  # noqa: E402
    build_mcp_config,
    configure_claude_code,
    configure_claude_desktop,
    configure_codex_mcp,
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


def test_configure_claude_code(tmp_path):
    claude_code_file = tmp_path / ".claude.json"
    initial = {"mcpServers": {"existing": {"command": "node"}}}
    claude_code_file.write_text(json.dumps(initial), encoding="utf-8")

    mcp_entry = build_mcp_config("http://localhost:8091", "token_abc")
    ok = configure_claude_code(claude_code_file, mcp_entry)
    assert ok is True

    result = json.loads(claude_code_file.read_text(encoding="utf-8"))
    assert "existing" in result["mcpServers"]
    assert "sensoryplex" in result["mcpServers"]
    assert result["mcpServers"]["sensoryplex"]["env"]["SENSORYPLEX_API_TOKEN"] == "token_abc"


def test_configure_codex_mcp(tmp_path):
    codex_config = tmp_path / "config.toml"
    initial_toml = """[some_table]
key = "value"
"""
    codex_config.write_text(initial_toml, encoding="utf-8")

    ok = configure_codex_mcp(codex_config, "http://localhost:8091", "token_xyz")
    assert ok is True

    text = codex_config.read_text(encoding="utf-8")
    assert "[mcp_servers.sensoryplex]" in text
    assert "[mcp_servers.sensoryplex.env]" in text
    assert "token_xyz" in text
    assert "[some_table]" in text
