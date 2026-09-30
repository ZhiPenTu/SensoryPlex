#!/usr/bin/env python3
"""SensoryPlex MCP & Skills Automated Setup Tool.

Automatically detects and configures:
1. Claude Desktop (claude_desktop_config.json)
2. Codex CLI & Desktop (~/.codex/skills/sensoryplex and codex mcp add)
3. Cursor / Windsurf MCP configuration

Usage:
    python tools/setup_mcp.py [--auto] [--dry-run] [--base-url URL] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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
    print("\n[1/3] Configuring Claude Desktop...")
    print(f"      Target file: {config_path}")

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


def install_codex_skill(codex_home: Path, dry_run: bool = False) -> bool:
    """Install the SensoryPlex skill to ~/.codex/skills/sensoryplex."""
    print("\n[2/3] Installing SensoryPlex Codex Skill...")
    skills_dir = codex_home / "skills"
    target_skill_dir = skills_dir / "sensoryplex"
    print(f"      Target skill directory: {target_skill_dir}")

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

        # Copy skill tree into target location
        shutil.copytree(SKILLS_SRC_DIR, target_skill_dir)
        print("      ✓ Successfully installed Codex skill 'sensoryplex'!")
        return True
    except Exception as exc:
        print(f"      ✗ Error installing Codex skill: {exc}")
        return False


def print_cli_instructions(base_url: str, token: str) -> None:
    """Print instructions for Codex CLI and Cursor integration."""
    print("\n[3/3] CLI & Alternative Assistants Setup:")
    print("      " + "─" * 60)
    print("      ▶ For Codex CLI (add as MCP server):")
    cmd = f"codex mcp add sensoryplex -- uv run --directory {MCP_SERVER_DIR} sensoryplex-mcp"
    print(f"        $ {cmd}")
    print()
    print("      ▶ For Cursor (.cursor/mcp.json):")
    cursor_config = {
        "mcpServers": {
            "sensoryplex": {
                "command": "uv",
                "args": ["run", "--directory", str(MCP_SERVER_DIR), "sensoryplex-mcp"],
                "env": {
                    "SENSORYPLEX_BASE_URL": base_url,
                    "SENSORYPLEX_API_TOKEN": token or "YOUR_TOKEN",
                },
            }
        }
    }
    print("        " + json.dumps(cursor_config, indent=2).replace("\n", "\n        "))
    print("      " + "─" * 60)


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
    parser.add_argument("--claude-only", action="store_true", help="Only configure Claude Desktop")
    parser.add_argument("--codex-only", action="store_true", help="Only configure Codex skill")
    args = parser.parse_args()

    api_port = read_env_value("API_PORT") or "8091"
    base_url = (
        args.base_url or os.getenv("SENSORYPLEX_BASE_URL") or f"http://127.0.0.1:{api_port}"
    )
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
    claude_config_path = get_claude_desktop_config_path()
    codex_home = get_codex_home()

    success = True
    if not args.codex_only:
        ok = configure_claude_desktop(claude_config_path, mcp_entry, dry_run=args.dry_run)
        success = success and ok

    if not args.claude_only:
        ok = install_codex_skill(codex_home, dry_run=args.dry_run)
        success = success and ok

    print_cli_instructions(base_url, token)

    if success:
        print("\n🎉 Setup complete! Restart Claude Desktop or Codex to use SensoryPlex tools.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
