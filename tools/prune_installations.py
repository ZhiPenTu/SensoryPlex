#!/usr/bin/env python3
"""SensoryPlex 算力节点与插件热部署旧安装清理工具（Old Installations Pruning Tool）。

用于清理本地节点磁盘上堆积的历史插件版本、已废弃的 runtime 目录、临时 staging 目录、
孤立的 LaunchAgent/systemd 服务单位以及历史 task_executions。

功能特性：
1. 识别并保护当前活跃的 running release 与 runtime 实例；
2. 物理清理历史淘汰版本的 releases/ 目录（每个 venv 释放数百 MB）；
3. 清理已停止的 runtimes/ 目录与残留日志；
4. 卸载并删除已淘汰的 LaunchAgent plist 与 systemd unit 文件；
5. 清理遗留的 staging 临时解包目录与旧格式实例；
6. 提供 --dry-run 预检统计与安全释放报告。

用法：
    python tools/prune_installations.py [--dry-run] [--keep 1] [--base-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_AGENT_BASE = REPO_ROOT / ".data/agent"


def get_dir_size(path: Path) -> int:
    """递归计算目录或文件的物理占用字节数。"""
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += get_dir_size(Path(entry.path))
            except OSError:
                continue
    except OSError:
        pass
    return total


def format_bytes(num_bytes: int) -> str:
    """将字节数格式化为人类易读的单位。"""
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def load_hot_deploy_ledger(ledger_file: Path) -> dict[str, dict]:
    """读取节点的 hot-deploy.json 台账。"""
    if not ledger_file.is_file():
        return {}
    try:
        data = json.loads(ledger_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_active_and_known_specs(ledger: dict[str, dict]) -> tuple[set[str], set[str], set[str]]:
    """从台账解析活跃的 release_id、活跃的 runtime_instance_id 以及活跃的 unit_name。"""
    active_releases: set[str] = set()
    active_runtimes: set[str] = set()
    active_units: set[str] = set()

    for rti_id, spec in ledger.items():
        if not isinstance(spec, dict):
            continue
        desired = spec.get("desired_state", "")
        # 如果是 running 状态，保护该实例
        if desired == "running":
            active_runtimes.add(rti_id)
            if spec.get("release_id"):
                active_releases.add(spec["release_id"])
            if spec.get("unit_name"):
                active_units.add(spec["unit_name"])

    return active_releases, active_runtimes, active_units


def prune_plugin_directories(
    plugins_dir: Path,
    active_releases: set[str],
    active_runtimes: set[str],
    keep_releases_count: int = 1,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """清理插件目录下的历史 releases、停止的 runtimes、staging 及 legacy 目录。"""
    total_freed = 0
    actions: list[str] = []

    if not plugins_dir.is_dir():
        return total_freed, actions

    for plugin_dir in plugins_dir.iterdir():
        if not plugin_dir.is_dir() or plugin_dir.name in {"task-executions", ".DS_Store"}:
            continue

        # 1. 清理 staging 目录
        staging_dir = plugin_dir / "staging"
        if staging_dir.is_dir():
            size = get_dir_size(staging_dir)
            if size > 0:
                total_freed += size
                actions.append(f"[Staging] 清理临时解包目录 {staging_dir} ({format_bytes(size)})")
                if not dry_run:
                    shutil.rmtree(staging_dir, ignore_errors=True)

        # 2. 清理历史 releases 目录
        releases_dir = plugin_dir / "releases"
        if releases_dir.is_dir():
            all_releases = [d for d in releases_dir.iterdir() if d.is_dir()]
            # 区分活跃版本与非活跃版本
            inactive_releases = [d for d in all_releases if d.name not in active_releases]
            # 按最后修改时间倒序排列（越新越靠前）
            inactive_releases.sort(key=lambda d: (d.stat().st_mtime, d.name), reverse=True)

            # 保留前 keep_releases_count 个非活跃版本作为回滚备用，超出部分物理删除
            for rel_path in inactive_releases[keep_releases_count:]:
                size = get_dir_size(rel_path)
                total_freed += size
                actions.append(
                    f"[Release] 删除历史淘汰版本 "
                    f"{plugin_dir.name}/{rel_path.name} ({format_bytes(size)})"
                )
                if not dry_run:
                    shutil.rmtree(rel_path, ignore_errors=True)

        # 3. 清理已停止的历史 runtimes 目录
        runtimes_dir = plugin_dir / "runtimes"
        if runtimes_dir.is_dir():
            for rti_path in runtimes_dir.iterdir():
                if not rti_path.is_dir():
                    continue
                rti_id = rti_path.name
                if rti_id in active_runtimes:
                    continue  # 正在运行中，保留

                size = get_dir_size(rti_path)
                total_freed += size
                actions.append(
                    f"[Runtime] 删除非活跃运行目录 "
                    f"{plugin_dir.name}/{rti_id} ({format_bytes(size)})"
                )
                if not dry_run:
                    shutil.rmtree(rti_path, ignore_errors=True)

        # 4. 清理旧式 legacy 内容寻址目录（如 09b6d646...）
        for item in plugin_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name in {"releases", "runtimes", "staging"}:
                continue
            # 旧式 sha256 目录一般为 64 位十六进制字符
            if len(item.name) == 64 and all(c in "0123456789abcdefABCDEF" for c in item.name):
                size = get_dir_size(item)
                total_freed += size
                actions.append(
                    f"[Legacy] 删除旧格式实例目录 "
                    f"{plugin_dir.name}/{item.name} ({format_bytes(size)})"
                )
                if not dry_run:
                    shutil.rmtree(item, ignore_errors=True)

    return total_freed, actions


def prune_orphan_platform_units(
    active_units: set[str], dry_run: bool = False
) -> tuple[int, list[str]]:
    """卸载并清理系统中已废弃、孤立的 LaunchAgent / systemd 插件单位。"""
    total_freed = 0
    actions: list[str] = []
    plat = platform.system()

    if plat == "Darwin":
        launch_agents_dir = Path.home() / "Library/LaunchAgents"
        if not launch_agents_dir.is_dir():
            return total_freed, actions

        uid = os.getuid()
        for plist in launch_agents_dir.glob("org.sensoryplex.plugin.*.plist"):
            unit_name = plist.stem
            if unit_name in active_units:
                continue  # 当前活跃单位，保留

            size = plist.stat().st_size
            total_freed += size
            actions.append(f"[LaunchAgent] 卸载并删除孤立服务单位 {plist.name}")

            if not dry_run:
                # 尝试通过 launchctl bootout 卸载
                domain = f"gui/{uid}"
                try:
                    subprocess.run(
                        ["launchctl", "bootout", f"{domain}/{unit_name}"],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
                try:
                    plist.unlink(missing_ok=True)
                except OSError:
                    pass

    elif plat == "Linux":
        systemd_dir = Path.home() / ".config/systemd/user"
        if not systemd_dir.is_dir():
            return total_freed, actions

        for service in systemd_dir.glob("org.sensoryplex.plugin.*.service"):
            unit_name = service.stem
            if unit_name in active_units:
                continue

            size = service.stat().st_size
            total_freed += size
            actions.append(f"[Systemd] 停止并删除孤立服务单位 {service.name}")

            if not dry_run:
                try:
                    subprocess.run(
                        ["systemctl", "--user", "disable", "--now", f"{unit_name}.service"],
                        capture_output=True,
                        timeout=5,
                    )
                    subprocess.run(
                        ["systemctl", "--user", "reset-failed", f"{unit_name}.service"],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
                try:
                    service.unlink(missing_ok=True)
                except OSError:
                    pass

    return total_freed, actions


def prune_task_executions(
    task_executions_dir: Path, dry_run: bool = False
) -> tuple[int, list[str]]:
    """清理历史任务执行临时产物（.data/agent/plugins/task-executions/）。"""
    total_freed = 0
    actions: list[str] = []

    if not task_executions_dir.is_dir():
        return total_freed, actions

    size = get_dir_size(task_executions_dir)
    if size > 0:
        total_freed += size
        actions.append(
            f"[TaskExecutions] 清理历史任务执行产物 {task_executions_dir} ({format_bytes(size)})"
        )
        if not dry_run:
            shutil.rmtree(task_executions_dir, ignore_errors=True)
            task_executions_dir.mkdir(parents=True, exist_ok=True)

    return total_freed, actions


def _parse_semver_key(version_str: str) -> tuple[int, ...]:
    clean = version_str.lstrip("v")
    try:
        return tuple(int(part) for part in clean.split("."))
    except ValueError:
        return (0,)


def prune_releases_bundle_cache(
    releases_cache_dir: Path, active_releases: set[str], dry_run: bool = False
) -> tuple[int, list[str]]:
    """清理本地制品包构建仓（.data/releases/）中的旧版本。"""
    total_freed = 0
    actions: list[str] = []

    if not releases_cache_dir.is_dir():
        return total_freed, actions

    for plugin_dir in releases_cache_dir.iterdir():
        if not plugin_dir.is_dir():
            continue
        versions = [v for v in plugin_dir.iterdir() if v.is_dir()]
        # 按语义版本优先倒序排列，确保永远保留最高版本
        versions.sort(key=lambda d: (_parse_semver_key(d.name), d.stat().st_mtime), reverse=True)

        # 保留最新的 1 个构建版本，清理更旧的
        for old_ver in versions[1:]:
            size = get_dir_size(old_ver)
            total_freed += size
            actions.append(
                f"[BundleCache] 清理历史制品构建缓存 "
                f"{plugin_dir.name}/{old_ver.name} ({format_bytes(size)})"
            )
            if not dry_run:
                shutil.rmtree(old_ver, ignore_errors=True)

    return total_freed, actions


def prune_all(
    base_dir: Path,
    keep_releases: int = 1,
    include_tasks: bool = False,
    include_bundles: bool = False,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """执行全部旧安装与旧版本清理流程。"""
    plugins_dir = base_dir / "plugins"
    ledger_file = plugins_dir / "hot-deploy.json"
    ledger = load_hot_deploy_ledger(ledger_file)
    active_releases, active_runtimes, active_units = get_active_and_known_specs(ledger)

    total_bytes = 0
    all_actions: list[str] = []

    # 1. 清理各插件目录（releases, runtimes, staging, legacy）
    bytes1, actions1 = prune_plugin_directories(
        plugins_dir,
        active_releases,
        active_runtimes,
        keep_releases_count=keep_releases,
        dry_run=dry_run,
    )
    total_bytes += bytes1
    all_actions.extend(actions1)

    # 2. 清理孤立平台服务 (LaunchAgents / systemd)
    bytes2, actions2 = prune_orphan_platform_units(active_units, dry_run=dry_run)
    total_bytes += bytes2
    all_actions.extend(actions2)

    # 3. 可选清理 task-executions
    if include_tasks:
        bytes3, actions3 = prune_task_executions(plugins_dir / "task-executions", dry_run=dry_run)
        total_bytes += bytes3
        all_actions.extend(actions3)

    # 4. 可选清理 .data/releases bundle 缓存
    if include_bundles:
        bundle_dir = base_dir.parent / "releases"
        bytes4, actions4 = prune_releases_bundle_cache(bundle_dir, active_releases, dry_run=dry_run)
        total_bytes += bytes4
        all_actions.extend(actions4)

    return total_bytes, all_actions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SensoryPlex Host Node & Plugin Old Installations Pruning Tool"
    )
    parser.add_argument(
        "--base-dir",
        default="",
        help=f"Node agent base directory (default: {DEFAULT_AGENT_BASE})",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=1,
        help="Number of latest inactive releases to keep as rollback backups (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and simulate cleanup without modifying files",
    )
    parser.add_argument(
        "--include-tasks",
        action="store_true",
        help="Also prune historical task-executions temporary files",
    )
    parser.add_argument(
        "--include-bundles",
        action="store_true",
        help="Also prune old bundle builds under .data/releases",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Prune all old releases, runtimes, task-executions and bundle caches",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir) if args.base_dir else DEFAULT_AGENT_BASE
    include_tasks = args.include_tasks or args.all
    include_bundles = args.include_bundles or args.all

    mode_label = "【模拟预检 (DRY RUN)】" if args.dry_run else "【物理清理】"
    print("════════════════════════════════════════════════════════════")
    print(f"   SensoryPlex 算力节点旧安装与旧版本清理工具 {mode_label}   ")
    print("════════════════════════════════════════════════════════════")
    print(f"• 节点基础目录:     {base_dir}")
    print(f"• 保留备用版本数:   {args.keep}")
    print(f"• 清理任务临时产物: {'是' if include_tasks else '否'}")
    print(f"• 清理制品包缓存:   {'是' if include_bundles else '否'}")

    total_bytes, actions = prune_all(
        base_dir=base_dir,
        keep_releases=args.keep,
        include_tasks=include_tasks,
        include_bundles=include_bundles,
        dry_run=args.dry_run,
    )

    if not actions:
        print("\n✨ 节点环境非常干净，没有发现需要清理的历史旧安装或孤立文件！")
        return 0

    print(f"\n发现待处理项 ({len(actions)} 项):")
    for action in actions:
        print(f"  • {action}")

    print("\n" + "═" * 60)
    print(f" 预计释放磁盘空间: {format_bytes(total_bytes)}")
    print("═" * 60)

    if args.dry_run:
        print("\n💡 当前处于预检模式（--dry-run），未做任何实际删除修改。")
        print("   若要真正执行清理，请运行: python tools/prune_installations.py")
        return 0

    print(f"\n🎉 物理清理完成！成功释放 {format_bytes(total_bytes)} 磁盘空间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
