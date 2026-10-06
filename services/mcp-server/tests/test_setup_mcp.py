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
    configure_local_project,
    install_codex_skill,
    uninstall_claude_code,
    uninstall_claude_desktop,
    uninstall_codex_mcp,
    uninstall_codex_skill,
    uninstall_local_project,
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


def test_configure_local_project(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()

    ok = configure_local_project(workspace, "http://localhost:8091", "token_local")
    assert ok is True

    # Check .codex/config.toml
    codex_toml = (workspace / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.sensoryplex]" in codex_toml
    assert "token_local" in codex_toml

    # Check .codex/skills/sensoryplex
    codex_skill = workspace / ".codex/skills/sensoryplex/SKILL.md"
    assert codex_skill.is_file()

    # Check .agents/skills/sensoryplex
    agents_skill = workspace / ".agents/skills/sensoryplex/SKILL.md"
    assert agents_skill.is_file()

    # Check .cursor/mcp.json
    cursor_json = json.loads((workspace / ".cursor/mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" in cursor_json["mcpServers"]

    # Check .mcp.json
    mcp_json = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" in mcp_json["mcpServers"]

    # Check .vscode/mcp.json
    vscode_json = json.loads((workspace / ".vscode/mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" in vscode_json["servers"]


def test_uninstall_claude_desktop(tmp_path):
    config_file = tmp_path / "claude_desktop_config.json"
    initial = {
        "mcpServers": {
            "other": {"command": "node"},
            "sensoryplex": {"command": "uv", "args": ["run"]},
        }
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    ok = uninstall_claude_desktop(config_file)
    assert ok is True

    result = json.loads(config_file.read_text(encoding="utf-8"))
    assert "other" in result["mcpServers"]
    assert "sensoryplex" not in result["mcpServers"]


def test_uninstall_claude_code(tmp_path):
    config_file = tmp_path / ".claude.json"
    initial = {
        "mcpServers": {
            "other": {"command": "node"},
            "sensoryplex": {"command": "uv"},
        }
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    ok = uninstall_claude_code(config_file)
    assert ok is True

    result = json.loads(config_file.read_text(encoding="utf-8"))
    assert "other" in result["mcpServers"]
    assert "sensoryplex" not in result["mcpServers"]


def test_uninstall_codex_mcp(tmp_path):
    config_file = tmp_path / "config.toml"
    initial = """[first_table]
key = "1"

[mcp_servers.sensoryplex]
command = "uv"
args = ["run", "sensoryplex-mcp"]

[mcp_servers.sensoryplex.env]
SENSORYPLEX_BASE_URL = "http://localhost:8091"

[last_table]
key = "2"
"""
    config_file.write_text(initial, encoding="utf-8")

    ok = uninstall_codex_mcp(config_file)
    assert ok is True

    text = config_file.read_text(encoding="utf-8")
    assert "[first_table]" in text
    assert "[last_table]" in text
    assert "[mcp_servers.sensoryplex" not in text
    assert "SENSORYPLEX_BASE_URL" not in text


def test_uninstall_codex_skill(tmp_path):
    codex_home = tmp_path / ".codex"
    skill_dir = codex_home / "skills/sensoryplex"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("dummy", encoding="utf-8")

    ok = uninstall_codex_skill(codex_home)
    assert ok is True
    assert not skill_dir.exists()


def test_uninstall_local_project(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()

    # 首先配置
    configure_local_project(workspace, "http://localhost:8091", "token_local")
    assert (workspace / ".codex/skills/sensoryplex").is_dir()
    assert (workspace / ".agents/skills/sensoryplex").is_dir()

    # 然后卸载清理
    ok = uninstall_local_project(workspace)
    assert ok is True

    # 验证技能目录已被物理删除
    assert not (workspace / ".codex/skills/sensoryplex").exists()
    assert not (workspace / ".agents/skills/sensoryplex").exists()

    # 验证配置文件中的条目已被移除
    codex_toml = (workspace / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.sensoryplex" not in codex_toml

    cursor_json = json.loads((workspace / ".cursor/mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" not in cursor_json.get("mcpServers", {})

    mcp_json = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" not in mcp_json.get("mcpServers", {})

    vscode_json = json.loads((workspace / ".vscode/mcp.json").read_text(encoding="utf-8"))
    assert "sensoryplex" not in vscode_json.get("servers", {})
