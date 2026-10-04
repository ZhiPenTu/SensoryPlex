"""测试节点旧安装与旧版本清理工具 (tools/prune_installations.py)。"""

import json
from pathlib import Path

from tools.prune_installations import prune_all


def test_prune_old_releases_preserves_active_and_prunes_old(tmp_path: Path):
    base_dir = tmp_path / "agent"
    plugins_dir = base_dir / "plugins"
    plugin_name = "org.sensoryplex.ocr-rapidocr"
    plugin_dir = plugins_dir / plugin_name
    releases_dir = plugin_dir / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    import os
    import time

    # 构造 3 个 release 目录，并模拟不同的创建时间（rel_backup 比 rel_old 新）
    rel_active = releases_dir / "rel_active"
    rel_backup = releases_dir / "rel_backup"
    rel_old = releases_dir / "rel_old"

    for r in (rel_active, rel_backup, rel_old):
        r.mkdir(parents=True, exist_ok=True)
        (r / "venv").mkdir()
        (r / "venv" / "dummy.txt").write_text("dummy " * 100)

    now = time.time()
    os.utime(rel_active, (now, now))
    os.utime(rel_backup, (now - 100, now - 100))
    os.utime(rel_old, (now - 1000, now - 1000))

    # 构造 hot-deploy.json，标记 rel_active 为 running
    ledger = {
        "rti_active": {
            "plugin_id": plugin_name,
            "release_id": "rel_active",
            "desired_state": "running",
            "unit_name": "org.sensoryplex.plugin.test.unit",
        }
    }
    (plugins_dir / "hot-deploy.json").write_text(json.dumps(ledger), encoding="utf-8")

    # 执行清理：keep_releases=1 (即保留 active + 最多 1 个备用，淘汰 rel_old)
    freed, actions = prune_all(base_dir=base_dir, keep_releases=1, dry_run=False)

    assert freed > 0
    assert len(actions) >= 1
    # rel_active 必须保留
    assert rel_active.is_dir()
    # rel_old 必须被物理删除
    assert not rel_old.exists()


def test_prune_staging_and_stopped_runtimes(tmp_path: Path):
    base_dir = tmp_path / "agent"
    plugin_dir = base_dir / "plugins/org.sensoryplex.test-plugin"
    staging_dir = plugin_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "temp_wheel.whl").write_bytes(b"dummy" * 500)

    runtimes_dir = plugin_dir / "runtimes"
    active_rti = runtimes_dir / "rti_active"
    stopped_rti = runtimes_dir / "rti_stopped"
    active_rti.mkdir(parents=True, exist_ok=True)
    stopped_rti.mkdir(parents=True, exist_ok=True)
    (stopped_rti / "endpoint.json").write_text("{}", encoding="utf-8")

    ledger = {
        "rti_active": {
            "plugin_id": "org.sensoryplex.test-plugin",
            "release_id": "rel_1",
            "desired_state": "running",
        },
        "rti_stopped": {
            "plugin_id": "org.sensoryplex.test-plugin",
            "release_id": "rel_0",
            "desired_state": "stopped",
        },
    }
    (base_dir / "plugins/hot-deploy.json").write_text(json.dumps(ledger), encoding="utf-8")

    freed, actions = prune_all(base_dir=base_dir, dry_run=False)

    assert freed > 0
    # staging 目录已清空
    assert not (staging_dir / "temp_wheel.whl").exists()
    # 活跃 runtime 必须保留
    assert active_rti.is_dir()
    # 停止的 runtime 必须被删除
    assert not stopped_rti.exists()


def test_prune_dry_run_preserves_everything(tmp_path: Path):
    base_dir = tmp_path / "agent"
    plugin_dir = base_dir / "plugins/org.sensoryplex.test-plugin"
    staging_dir = plugin_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    temp_file = staging_dir / "temp.txt"
    temp_file.write_text("hello world")

    # dry-run 执行
    freed, actions = prune_all(base_dir=base_dir, dry_run=True)

    assert freed > 0
    assert len(actions) >= 1
    # 文件仍安然无恙
    assert temp_file.is_file()
