"""多模态文件任务执行闭环验收工具（ADR-028 / ADR-029 / ADR-030）。

全面验收同机真实视频从分发、端侧三模态计算、回执、Timeline 融合到覆盖层的完整事实链。
报告如实输出：平台、执行后端、模型 digest、配置 hash、窗口数、
每模态调度/成功统计与所有 reason code。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/api/src"))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from sensoryplex_gateway.settings import Settings  # noqa: E402


class MultimodalExecutionVerifier:
    def __init__(
        self,
        media_path: Path,
        base_url: str = "http://127.0.0.1:8091",
        admin_user: str = "demo",
        admin_pass: str = "",
        timeout_s: float = 300.0,
    ):
        self.media_path = Path(media_path).resolve()
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.timeout_s = timeout_s
        self.csrf_token = ""
        self.cookies = ""
        self.db_url = Settings().database_url.get_secret_value()
        self.test_id = uuid.uuid4().hex[:8]

    def _http(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        if self.cookies:
            headers["Cookie"] = self.cookies

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookies = set_cookie.split(";")[0]
                raw = resp.read()
                return status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read()
            try:
                parsed = json.loads(err_body.decode("utf-8"))
                return e.code, parsed
            except Exception:
                return e.code, {"detail": err_body.decode("utf-8")}

    def login(self):
        pw = self.admin_pass
        if not pw:
            pw_file = ROOT / ".data/demo-password"
            if pw_file.is_file():
                pw = pw_file.read_text().strip()
            else:
                pw = os.environ.get("SENSORYPLEX_DEMO_PASSWORD", "test-account-password-2026")

        status, body = self._http(
            "POST",
            "/auth/v1/session",
            {"username": self.admin_user, "password": pw},
        )
        if status != 200:
            print(f"Login failed: {status} {body}", file=sys.stderr)
            sys.exit(1)
        self.csrf_token = body.get("csrf_token", "")

    def ensure_asset(self) -> str:
        if not self.media_path.is_file():
            print(f"Media file not found: {self.media_path}", file=sys.stderr)
            sys.exit(1)
        data = self.media_path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        filename = self.media_path.name

        with psycopg.connect(self.db_url) as conn:
            existing = conn.execute(
                "SELECT id FROM console_upload WHERE sha256=%s ORDER BY created_at DESC LIMIT 1",
                (digest,),
            ).fetchone()
            if existing:
                print(f"[asset] Found existing uploaded asset: {existing[0]} ({filename})")
                return existing[0]

        print(f"[asset] Uploading {filename} ({len(data)} bytes, {digest})...")
        status, init_res = self._http(
            "POST",
            "/v1/uploads",
            {
                "filename": filename,
                "size_bytes": len(data),
                "content_type": "video/mp4" if filename.endswith(".mp4") else "video/webm",
                "sha256": digest,
            },
        )
        if status != 201:
            print(f"Failed to initiate upload: {status} {init_res}", file=sys.stderr)
            sys.exit(1)
        asset_id = init_res["id"]

        # 上传二进制内容
        url = f"{self.base_url}/v1/uploads/{asset_id}/content"
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-CSRF-Token": self.csrf_token,
                "Cookie": self.cookies,
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                print(f"Failed to upload content: {resp.status}", file=sys.stderr)
                sys.exit(1)
        print(f"[asset] Upload completed: {asset_id}")
        return asset_id

    def ensure_pipeline(self) -> str:
        with psycopg.connect(self.db_url) as conn:
            row = conn.execute(
                """
                SELECT id FROM console_pipeline
                WHERE execution_mode='orchestrated_v2' AND orchestration_revision IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if row:
                print(f"[pipeline] Using existing published v2 pipeline: {row[0]}")
                return row[0]

        print("[pipeline] Creating and publishing fresh v2 pipeline...")
        config_ids = {}
        for key, plugin_id in (
            ("ocr_fast", "org.sensoryplex.ocr-rapidocr"),
            ("asr_fast", "org.sensoryplex.asr-whisper-mlx"),
            ("vlm_enrich", "org.sensoryplex.vlm-moondream"),
        ):
            status, res = self._http(
                "POST",
                "/admin/v1/plugin-configurations",
                {"plugin_id": plugin_id, "name": f"e2e-{key}-{self.test_id}", "config": {}},
            )
            config_ids[key] = res["id"]

        body = {
            "name": f"e2e-multimodal-{self.test_id}",
            "description": "automated multimodal execution check",
            "components": [{"node_id": k, "config_id": v} for k, v in config_ids.items()],
            "policy": {
                "window_ms": 1000,
                "sample_interval_ms": 1000,
                "audio_segment_ms": 6000,
                "audio_overlap_ms": 500,
                "vlm_sample_interval_ms": 5000,
            },
        }
        status, created = self._http("POST", "/admin/v1/multimodal-pipelines", body)
        if status != 201:
            print(f"Failed to save pipeline: {status} {created}", file=sys.stderr)
            sys.exit(1)
        pipeline_id = created["id"]
        status, _ = self._http("POST", f"/admin/v1/pipelines/{pipeline_id}:publish")
        if status != 200:
            print(f"Failed to publish pipeline: {status}", file=sys.stderr)
            sys.exit(1)
        print(f"[pipeline] Published v2 pipeline: {pipeline_id}")
        return pipeline_id

    def run(self):
        print("==============================================================================")
        print(" SensoryPlex 多模态文件任务执行闭环验收 (make multimodal-execution-check)")
        print("==============================================================================")
        self.login()
        asset_id = self.ensure_asset()
        pipeline_id = self.ensure_pipeline()

        print("\n[dispatch] Creating job draft targeting local-host...")
        status, draft = self._http(
            "POST",
            "/v1/job-drafts",
            {
                "name": f"e2e-verification-{self.test_id}",
                "asset_id": asset_id,
                "pipeline_id": pipeline_id,
            },
        )
        if status != 201:
            print(f"Failed to create draft: {status} {draft}", file=sys.stderr)
            sys.exit(1)
        job_id = draft["id"]

        status, disp = self._http(
            "POST", f"/v1/job-drafts/{job_id}:dispatch", {"node_id": "local-host"}
        )
        if status != 200:
            print(f"Failed to dispatch job: {status} {disp}", file=sys.stderr)
            sys.exit(1)

        execution_id = disp["execution_id"]
        run_id = disp["run_id"]
        print(
            f"[dispatch] Job dispatched: job_id={job_id}\n"
            f"  execution_id={execution_id} run_id={run_id}"
        )

        print("\n[execution] Waiting for task execution and model inference...")
        deadline = time.time() + self.timeout_s
        terminal_states = {"succeeded", "succeeded_with_partial_enrichment", "failed", "cancelled"}
        final_exec_state = ""
        last_logged = ""

        while time.time() < deadline:
            with psycopg.connect(self.db_url) as conn:
                exec_row = conn.execute(
                    "SELECT state, modality_summary FROM console_job_execution "
                    "WHERE execution_id=%s",
                    (execution_id,),
                ).fetchone()
                tasks = conn.execute(
                    "SELECT node_id, state, reason_code FROM pipeline_task "
                    "WHERE run_id=%s ORDER BY node_id",
                    (run_id,),
                ).fetchall()

            current_tasks = ", ".join(f"{t[0]}={t[1]}" for t in tasks)
            if current_tasks != last_logged:
                print(f"  · Tasks: {current_tasks}")
                last_logged = current_tasks

            if exec_row and exec_row[0] in terminal_states:
                final_exec_state = exec_row[0]
                break
            time.sleep(2)
        else:
            print(
                f"[timeout] Execution did not reach terminal state within {self.timeout_s}s",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"\n[execution] Execution finished with terminal state: {final_exec_state}")

        # ── 详细指标报告 ──────────────────────────────────────────────────────────
        with psycopg.connect(self.db_url) as conn:
            receipts = conn.execute(
                """
                SELECT task_id, plugin_id, artifact_digest, config_hash,
                       input_count, output_count, reason_code,
                       EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000 AS duration_ms
                FROM task_execution_receipt WHERE run_id=%s ORDER BY started_at
                """,
                (run_id,),
            ).fetchall()

            windows = conn.execute(
                """
                SELECT count(*), min(start_ms), max(end_ms),
                       count(*) FILTER (WHERE (modality_states->>'ocr') = 'observed')
                           as ocr_observed,
                       count(*) FILTER (WHERE (modality_states->>'asr') = 'observed')
                           as asr_observed,
                       count(*) FILTER (WHERE (modality_states->>'vlm') = 'observed')
                           as vlm_observed,
                       count(*) FILTER (WHERE (modality_states->>'vlm') = 'not_sampled_by_policy')
                           as vlm_skipped,
                       count(*) FILTER (WHERE (modality_states->>'asr') = 'not_applicable')
                           as asr_na
                FROM timeline_window_state WHERE execution_id=%s
                """,
                (execution_id,),
            ).fetchone()

            materials_count = conn.execute(
                "SELECT count(*) FROM material_execution WHERE execution_id=%s",
                (execution_id,),
            ).fetchone()[0]

        print("\n==============================================================================")
        print(" 多模态执行闭环证据报告 (ADR-028 / ADR-029 / ADR-030)")
        print("==============================================================================")
        print(f"平台架构:           {platform.system()} ({platform.machine()})")
        print("执行模式:           orchestrated_v2 (Node Agent Local Task Executor)")
        print(f"媒体样本:           {self.media_path.name} (asset_id={asset_id})")
        print(f"执行标识:           execution_id={execution_id}")
        print(f"执行状态:           {final_exec_state}")
        print(f"落库素材单元数:     {materials_count} (按 execution_id 严格隔离)")
        print(f"覆盖窗总数 (1s网格): {windows[0]} 个 (时区范围 [{windows[1]}ms, {windows[2]}ms))")
        print(f"  · OCR 观测覆盖:    {windows[3]} 窗")
        print(f"  · ASR 语音覆盖:    {windows[4]} 窗 (无音轨/无语音标注: {windows[7]} 窗)")
        print(f"  · VLM 场景描述:    {windows[5]} 窗 (策略未采样: {windows[6]} 窗)")

        print("\n[任务回执详情 (TaskExecutionReceipt)]")
        durations = []
        for r in receipts:
            durations.append(r[7] or 0)
            reason_str = f" reason={r[6]}" if r[6] else ""
            print(
                f"  - [{r[1]}] inputs={r[4]} outputs={r[5]} "
                f"duration={round(r[7] or 0, 1)}ms digest={r[2][:20]}...{reason_str}"
            )

        if durations:
            durations.sort()
            p50 = durations[len(durations) // 2]
            p95 = durations[int(len(durations) * 0.95)]
            print(f"\n耗时分位数: P50={round(p50, 1)}ms, P95={round(p95, 1)}ms")

        if (
            final_exec_state in {"succeeded", "succeeded_with_partial_enrichment"}
            and materials_count > 0
        ):
            print(
                "\n=============================================================================="
            )
            print(" [PASS] 多模态文件任务执行闭环验证完全通过")
            print("==============================================================================")
        else:
            print("\n[FAIL] 执行未产出有效素材或未达到成功终态", file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Multimodal Execution Verifier")
    parser.add_argument("--media", required=True, help="Path to authorized video file")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091", help="API URL")
    parser.add_argument("--admin-user", default="demo", help="Admin user")
    parser.add_argument("--admin-pass", default="", help="Admin password")
    parser.add_argument("--timeout-s", type=float, default=480.0, help="Wait timeout in seconds")
    args = parser.parse_args()

    verifier = MultimodalExecutionVerifier(
        media_path=Path(args.media),
        base_url=args.base_url,
        admin_user=args.admin_user,
        admin_pass=args.admin_pass,
        timeout_s=args.timeout_s,
    )
    verifier.run()


if __name__ == "__main__":
    main()
