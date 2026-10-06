"""任务执行器（Agent 侧）：调度 Runtime 解码、多模态插件推理、Timeline 融合与素材入库。"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

from tools import timeline_handoff  # noqa: E402
from tools.verify_timeline_handoff import (  # noqa: E402
    fuse_timeline,
    replay_pass,
)
from tools.verify_timeline_semantic import (  # noqa: E402
    OCR_MODULE,
    derive_shared_url,
    ocr_model_dir,
)

PIPELINE = ROOT / "config/pipelines/file-material.yaml"


def run_video_task(config: dict, database_url: str = "") -> dict:
    job_id = config.get("job_id", "")
    asset_id = config.get("asset_id", "")
    sha256 = config.get("sha256", "")
    owner = config.get("owner", "local-developer")
    blob_path_str = config.get("blob_path", "")

    media_path = pathlib.Path(blob_path_str)
    if not media_path.is_file():
        digest_suffix = sha256.split(":")[-1] if ":" in sha256 else sha256
        fallback = ROOT / ".data/console-media" / digest_suffix
        if fallback.is_file():
            media_path = fallback
        else:
            raise RuntimeError(f"media_file_not_found: {blob_path_str}")

    db_url, _ = derive_shared_url(database_url)
    workdir = ROOT / ".data/tasks" / job_id
    workdir.mkdir(parents=True, exist_ok=True)

    failures = []

    # legacy_ocr_v1 只保留 OCR 兼容事实。VLM 只能经 `orchestrated_v2` 的快路径入库事务创建
    # WorkQueue outbox；这里再同步全片解码会重新把慢路径变成用户的阻塞条件。
    model_dir = ocr_model_dir()
    ocr_pass = replay_pass(
        media_path,
        workdir,
        failures,
        plugin_module=OCR_MODULE,
        report_name="replay-ocr.pb",
        worker_report_name="ai-worker-ocr.json",
        plugin_config={
            "provider": "cpu",
            "model_dir": str(model_dir),
            "text_score": 0.5,
            "ttl_ms": 30_000,
            "timeout_s": 1800.0,
        },
        label="ocr",
        max_frames=int(config.get("max_frames", 30)),
        exact_frame_count=False,
    )
    merged_report = ocr_pass
    replay_report = workdir / "replay-ocr.pb"

    worker_report_path = workdir / "ai-worker-merged.json"
    worker_report_path.write_text(json.dumps(merged_report, indent=2, ensure_ascii=False))

    # 2. 运行 Timeline 融合核心（确定性栅格选窗，逐条准入）
    fuse_timeline(
        media_path,
        workdir,
        replay_report=replay_report,
        worker_report=worker_report_path,
        failures=failures,
        material_dir_name="materials",
        report_name="timeline.json",
    )

    if failures:
        raise RuntimeError(f"pipeline_fusion_failed: {'; '.join(failures)}")

    # 3. 授权事务性追加到数据库与 outbox
    timeline_report_path = workdir / "timeline.json"
    material_dir = workdir / "materials"

    handoff_result = timeline_handoff.handoff(
        report_path=timeline_report_path,
        material_dir=material_dir,
        owner=owner,
        trace_id=f"task:{job_id}",
        database_url=db_url,
        upload_id=asset_id,
    )

    handoff_path = workdir / "handoff.json"
    handoff_path.write_text(json.dumps(handoff_result, indent=2, ensure_ascii=False))

    return {
        "job_id": job_id,
        "asset_id": asset_id,
        "materials_count": handoff_result["counters"]["materials"],
        "appended": handoff_result["counters"]["appended"],
        "outbox_events": handoff_result["counters"]["outbox_events"],
    }
