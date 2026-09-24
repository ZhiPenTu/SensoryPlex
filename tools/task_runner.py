"""任务执行器（Agent 侧）：调度 Runtime 解码、多模态插件推理、Timeline 融合与素材入库。"""

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

from tools import timeline_handoff  # noqa: E402
from tools.verify_timeline_handoff import (  # noqa: E402
    fuse_timeline,
    replay_pass,
)
from tools.verify_timeline_semantic import (  # noqa: E402
    OCR_MODULE,
    VLM_MODULE,
    derive_shared_url,
    merge_worker_reports,
    ocr_model_dir,
)

PIPELINE = ROOT / "config/pipelines/file-material.yaml"


def is_ollama_ready() -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    passes = []

    # 1. 运行 OCR 识别遍（文字事实，为后续向量化和精准检索提供基石）
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
        max_frames=12,
    )
    passes.append(ocr_pass)

    # 2. 如果显式配置 with_vlm 且本机 Ollama 在线，运行 VLM 场景描述遍
    if config.get("with_vlm") and is_ollama_ready():
        try:
            vlm_pass = replay_pass(
                media_path,
                workdir,
                failures,
                plugin_module=VLM_MODULE,
                report_name="replay-vlm.pb",
                worker_report_name="ai-worker-vlm.json",
                label="vlm",
                max_frames=12,
                allow_frame_failures=True,
            )
            passes.append(vlm_pass)
        except Exception as e:
            print(f"[task_runner] VLM optional pass skipped: {e}")

    # 合并 worker 报告
    if len(passes) == 1:
        merged_report = passes[0]
        replay_report = workdir / "replay-ocr.pb"
    else:
        merged_report = merge_worker_reports(passes)
        replay_report = workdir / "replay-ocr.pb"

    worker_report_path = workdir / "ai-worker-merged.json"
    worker_report_path.write_text(json.dumps(merged_report, indent=2, ensure_ascii=False))

    # 3. 运行 Timeline 融合核心（确定性栅格选窗，逐条准入）
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

    # 4. 授权事务性追加到数据库与 outbox
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
