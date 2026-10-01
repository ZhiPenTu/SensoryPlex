#!/usr/bin/env python3
"""SensoryPlex MCP & Skills Automated Setup Tool.

Automatically detects and configures:
1. Claude Desktop (claude_desktop_config.json)
2. Claude Code CLI (~/.claude.json)
3. Codex CLI & Desktop (~/.codex/config.toml and ~/.codex/skills/sensoryplex)
4. Cursor / Windsurf (.cursor/mcp.json)

Usage:
    python tools/setup_mcp.py [--auto] [--dry-run] [--base-url URL] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = REPO_ROOT / "services/mcp-server"
SKILLS_SRC_DIR = REPO_ROOT / "skills/sensoryplex"


def read_env_value(key: str, env_file: Path | None = None) -> str:
    """Read value from current environment or fallback to repo .env file."""
    val = os.getenv(key, "")
    if val:
        return val
    env_path = env_file or (REPO_ROOT / ".env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                raw = line.split("=", 1)[1].strip()
                is_quoted = (raw.startswith('"') and raw.endswith('"')) or (
                    raw.startswith("'") and raw.endswith("'")
                )
                if is_quoted:
                    raw = raw[1:-1]
                return raw
    return ""


def get_claude_desktop_config_path() -> Path:
    """Locate Claude Desktop configuration file based on operating system."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif system == "Windows":
        appdata = os.getenv("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(appdata) / "Claude/claude_desktop_config.json"
    else:  # Linux
        return Path.home() / ".config/Claude/claude_desktop_config.json"


def get_claude_code_config_path() -> Path:
    """Locate Claude Code CLI configuration file."""
    return Path.home() / ".claude.json"


def get_codex_home() -> Path:
    """Locate Codex configuration and skills home directory."""
    custom = os.getenv("CODEX_HOME")
    if custom:
        return Path(custom)
    return Path.home() / ".codex"


def build_mcp_config(base_url: str, token: str) -> dict:
    """Construct standard MCP server configuration block."""
    env_vars: dict[str, str] = {
        "SENSORYPLEX_BASE_URL": base_url,
    }
    if token:
        env_vars["SENSORYPLEX_API_TOKEN"] = token

    return {
        "command": "uv",
        "args": [
            "run",
            "--directory",
            str(MCP_SERVER_DIR),
            "sensoryplex-mcp",
        ],
        "env": env_vars,
    }


def configure_claude_desktop(config_path: Path, mcp_entry: dict, dry_run: bool = False) -> bool:
    """Safely merge sensoryplex MCP configuration into Claude Desktop config."""
    print(f"\n[1/5] Configuring Claude Desktop ({config_path})...")

    existing_data: dict = {}
    if config_path.is_file():
        try:
            existing_data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"      Warning: Failed to parse existing Claude config ({exc}). Backing up.")
            if not dry_run:
                shutil.copy2(config_path, config_path.with_suffix(".json.bak"))

    if not isinstance(existing_data, dict):
        existing_data = {}

    servers = existing_data.setdefault("mcpServers", {})
    servers["sensoryplex"] = mcp_entry

    if dry_run:
        print("      [DRY RUN] Would write the following to Claude Desktop config:")
        print(json.dumps(existing_data, indent=2))
        return True

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(existing_data, indent=2, ensure_ascii=False) + "\n"
        config_path.write_text(payload, encoding="utf-8")
        print("      ✓ Successfully updated Claude Desktop configuration!")
        return True
    except Exception as exc:
        print(f"      ✗ Error writing Claude Desktop config: {exc}")
        return False


def configure_claude_code(config_path: Path, mcp_entry: dict, dry_run: bool = False) -> bool:
    """Safely merge sensoryplex MCP configuration into Claude Code CLI (~/.claude.json)."""
    print(f"\n[2/5] Configuring Claude Code CLI ({config_path})...")

    if not config_path.is_file():
        print("      Claude Code CLI configuration file not found, skipping.")
        return True

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print("      Invalid ~/.claude.json format, skipping.")
            return False

        servers = data.setdefault("mcpServers", {})
        servers["sensoryplex"] = mcp_entry

        if dry_run:
            print("      [DRY RUN] Would add sensoryplex to ~/.claude.json mcpServers")
            return True

        # Backup first
        shutil.copy2(config_path, config_path.with_suffix(".json.bak"))
        config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print("      ✓ Successfully updated Claude Code CLI configuration (~/.claude.json)!")
        return True
    except Exception as exc:
        print(f"      ✗ Error writing Claude Code config: {exc}")
        return False


def configure_codex_mcp(
    codex_config_path: Path, base_url: str, token: str, dry_run: bool = False
) -> bool:
    """Safely merge sensoryplex MCP server into Codex configuration (~/.codex/config.toml)."""
    print(f"\n[3/5] Configuring Codex MCP Server ({codex_config_path})...")

    args_list = ["run", "--directory", str(MCP_SERVER_DIR), "sensoryplex-mcp"]
    args_toml = json.dumps(args_list)

    snippet_lines = [
        "[mcp_servers.sensoryplex]",
        'command = "uv"',
        f"args = {args_toml}",
        "",
        "[mcp_servers.sensoryplex.env]",
        f'SENSORYPLEX_BASE_URL = "{base_url}"',
    ]
    if token:
        snippet_lines.append(f'SENSORYPLEX_API_TOKEN = "{token}"')
    snippet = "\n".join(snippet_lines) + "\n"

    if not codex_config_path.is_file():
        if dry_run:
            print("      [DRY RUN] Would create ~/.codex/config.toml with sensoryplex MCP")
            return True
        try:
            codex_config_path.parent.mkdir(parents=True, exist_ok=True)
            codex_config_path.write_text(snippet, encoding="utf-8")
            print("      ✓ Successfully created Codex config with sensoryplex MCP!")
            return True
        except Exception as exc:
            print(f"      ✗ Error creating Codex config: {exc}")
            return False

    text = codex_config_path.read_text(encoding="utf-8")
    section_header = "[mcp_servers.sensoryplex]"

    if section_header in text:
        pattern = r"\[mcp_servers\.sensoryplex\]\n.*?(?=\n\[(?!mcp_servers\.sensoryplex\.env)|\Z)"
        new_text = re.sub(pattern, snippet.strip(), text, flags=re.DOTALL)
    else:
        new_text = text.rstrip() + "\n\n" + snippet

    if dry_run:
        print("      [DRY RUN] Would update ~/.codex/config.toml with:")
        print("      " + snippet.replace("\n", "\n      "))
        return True

    try:
        shutil.copy2(codex_config_path, codex_config_path.with_suffix(".toml.bak"))
        codex_config_path.write_text(new_text, encoding="utf-8")
        print("      ✓ Successfully updated Codex configuration (~/.codex/config.toml)!")
        return True
    except Exception as exc:
        print(f"      ✗ Error updating Codex config: {exc}")
        return False


def install_codex_skill(codex_home: Path, dry_run: bool = False) -> bool:
    """Install the SensoryPlex skill to ~/.codex/skills/sensoryplex."""
    print(f"\n[4/5] Installing SensoryPlex Codex Skill ({codex_home / 'skills/sensoryplex'})...")
    skills_dir = codex_home / "skills"
    target_skill_dir = skills_dir / "sensoryplex"

    if not SKILLS_SRC_DIR.is_dir():
        print(f"      ✗ Error: Source skill directory {SKILLS_SRC_DIR} not found.")
        return False

    if dry_run:
        print(f"      [DRY RUN] Would copy {SKILLS_SRC_DIR} to {target_skill_dir}")
        return True

    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        if target_skill_dir.exists():
            if target_skill_dir.is_symlink():
                target_skill_dir.unlink()
            elif target_skill_dir.is_dir():
                shutil.rmtree(target_skill_dir)

        shutil.copytree(SKILLS_SRC_DIR, target_skill_dir)
        print("      ✓ Successfully installed Codex skill 'sensoryplex'!")
        return True
    except Exception as exc:
        print(f"      ✗ Error installing Codex skill: {exc}")
        return False


def configure_cursor_mcp(workspace_root: Path, mcp_entry: dict, dry_run: bool = False) -> bool:
    """Write or update workspace .cursor/mcp.json."""
    cursor_dir = workspace_root / ".cursor"
    cursor_config_file = cursor_dir / "mcp.json"
    print(f"\n[5/5] Configuring Cursor Workspace MCP ({cursor_config_file})...")

    existing_data: dict = {}
    if cursor_config_file.is_file():
        try:
            existing_data = json.loads(cursor_config_file.read_text(encoding="utf-8"))
        except Exception:
            existing_data = {}

    if not isinstance(existing_data, dict):
        existing_data = {}

    servers = existing_data.setdefault("mcpServers", {})
    servers["sensoryplex"] = mcp_entry

    if dry_run:
        print("      [DRY RUN] Would write .cursor/mcp.json")
        return True

    try:
        cursor_dir.mkdir(parents=True, exist_ok=True)
        cursor_config_file.write_text(
            json.dumps(existing_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print("      ✓ Successfully wrote .cursor/mcp.json!")
        return True
    except Exception as exc:
        print(f"      ✗ Error writing .cursor/mcp.json: {exc}")
        return False


def print_cli_summary(base_url: str, token: str) -> None:
    """Print quick reference commands for CLI and alternative tools."""
    print("\n" + "═" * 60)
    print("   AI Assistants Quick Reference / 快速使用指南")
    print("═" * 60)
    print("▶ 1. Codex CLI / Desktop:")
    print("   - MCP 协议配置已直接写入: ~/.codex/config.toml [mcp_servers.sensoryplex]")
    print("   - AI 技能已安装至:       ~/.codex/skills/sensoryplex")
    print("   - 可用命令行测试 MCP:     codex mcp list")
    print()
    print("▶ 2. Claude Desktop & Claude Code CLI:")
    print("   - Claude Desktop:      已写入 ~/Library/.../claude_desktop_config.json")
    print("   - Claude Code CLI:      已写入 ~/.claude.json (mcpServers.sensoryplex)")
    print("   - 可用命令行测试 MCP:     claude mcp list")
    print()
    print("▶ 3. Cursor / Windsurf:")
    print("   - 工作区配置已写入:       .cursor/mcp.json")
    print("═" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="SensoryPlex MCP and Skills Setup Tool")
    parser.add_argument(
        "--auto", action="store_true", help="Automatically configure without prompts"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without modifying files"
    )
    parser.add_argument("--base-url", default="", help="SensoryPlex API base URL")
    parser.add_argument("--token", default="", help="SensoryPlex API Bearer token")
    parser.add_argument(
        "--claude-only", action="store_true", help="Only configure Claude (Desktop and CLI)"
    )
    parser.add_argument(
        "--codex-only", action="store_true", help="Only configure Codex (MCP and Skill)"
    )
    parser.add_argument("--cursor-only", action="store_true", help="Only configure Cursor")
    args = parser.parse_args()

    api_port = read_env_value("API_PORT") or "8091"
    base_url = args.base_url or os.getenv("SENSORYPLEX_BASE_URL") or f"http://127.0.0.1:{api_port}"
    token = args.token or read_env_value("SENSORYPLEX_API_TOKEN")

    print("════════════════════════════════════════════════════════════")
    print("   SensoryPlex MCP Server & AI Skills Automated Setup      ")
    print("════════════════════════════════════════════════════════════")
    print(f"• Repo Root:       {REPO_ROOT}")
    print(f"• MCP Server:      {MCP_SERVER_DIR}")
    print(f"• API Base URL:    {base_url}")
    token_label = "[Configured]" if token else "[Not Set - Will use local demo fallback]"
    print(f"• API Token:       {token_label}")

    mcp_entry = build_mcp_config(base_url, token)
    claude_desktop_config = get_claude_desktop_config_path()
    claude_code_config = get_claude_code_config_path()
    codex_home = get_codex_home()
    codex_config = codex_home / "config.toml"

    success = True
    only_specified = args.claude_only or args.codex_only or args.cursor_only

    if not only_specified or args.claude_only:
        ok1 = configure_claude_desktop(claude_desktop_config, mcp_entry, dry_run=args.dry_run)
        ok2 = configure_claude_code(claude_code_config, mcp_entry, dry_run=args.dry_run)
        success = success and ok1 and ok2

    if not only_specified or args.codex_only:
        ok3 = configure_codex_mcp(codex_config, base_url, token, dry_run=args.dry_run)
        ok4 = install_codex_skill(codex_home, dry_run=args.dry_run)
        success = success and ok3 and ok4

    if not only_specified or args.cursor_only:
        ok5 = configure_cursor_mcp(REPO_ROOT, mcp_entry, dry_run=args.dry_run)
        success = success and ok5

    print_cli_summary(base_url, token)

    if success:
        print("\n🎉 Setup complete! Restart Claude Desktop or Codex to use SensoryPlex tools.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
