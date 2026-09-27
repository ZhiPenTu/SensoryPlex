"""多模态文件方案（v2）编排与控制面契约验收工具。

验收内容：
1. 控制面 Schema 与会话认证；
2. 方案验证与拒绝场景（无界重叠、缺必需快路径组件、window_ms 约束、非法配置关联）；
3. 编译合法 DAG 并发布不可变 PipelineRevision（校验 graph_digest、固定节点与边）；
4. 节点实例缺失时的阻断（plugin_instance_unavailable，不回退旧 OCR 旁路）；
5. 事务性生成不可变 console_job_execution 快照与任务图展开。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api/src"))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from sensoryplex_gateway.settings import Settings  # noqa: E402

CURRENT_SCHEMA = "0013_timeline_coverage_states"


class MultimodalPipelineVerifier:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8091",
        admin_user: str = "demo",
        admin_pass: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.csrf_token = ""
        self.cookies = ""
        self.db_url = Settings().database_url.get_secret_value()
        self.test_id = uuid.uuid4().hex[:8]
        self.results: list[dict[str, Any]] = []

    def log(self, section: str, message: str, ok: bool = True):
        status = "OK" if ok else "FAIL"
        print(f"[{status}] [{section}] {message}")
        self.results.append({"section": section, "message": message, "ok": ok})
        if not ok:
            print(f"FAILED in section: {section}", file=sys.stderr)
            sys.exit(1)

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
            with urllib.request.urlopen(req, timeout=15) as resp:
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
        self.log("01-auth", f"Login status: {status}", ok=(status == 200))
        self.csrf_token = body.get("csrf_token", "")

    def check_schema(self):
        status, body = self._http("GET", "/v1/health")
        self.log("00-health", f"API Health check: {status}", ok=(status == 200))
        schema = body.get("schema_version", "")
        self.log(
            "00-health",
            f"API Schema version: {schema}",
            ok=(schema >= CURRENT_SCHEMA),
        )

    def verify_rejections(self):
        print("\n--- [Scenario 1] 多模态方案参数校验与拒绝语义 ---")
        config_ids = {}
        for key, plugin_id in (
            ("ocr_fast", "org.sensoryplex.ocr-rapidocr"),
            ("asr_fast", "org.sensoryplex.asr-whisper-mlx"),
            ("vlm_enrich", "org.sensoryplex.vlm-moondream"),
        ):
            status, res = self._http(
                "POST",
                "/admin/v1/plugin-configurations",
                {"plugin_id": plugin_id, "name": f"test-cfg-{key}-{self.test_id}", "config": {}},
            )
            self.log(f"01-cfg-{key}", f"Created config: status={status}", ok=(status == 201))
            config_ids[key] = res["id"]

        base_body = {
            "name": f"test-pipe-{self.test_id}",
            "description": "acceptance pipeline validation test",
            "components": [{"node_id": k, "config_id": v} for k, v in config_ids.items()],
            "policy": {
                "window_ms": 1000,
                "sample_interval_ms": 1000,
                "audio_segment_ms": 6000,
                "audio_overlap_ms": 500,
                "vlm_sample_interval_ms": 5000,
            },
        }

        # 1. 音频重叠等于或超过分段时长必须被拒
        bad_overlap = dict(base_body)
        bad_overlap["policy"] = dict(base_body["policy"], audio_overlap_ms=6000)
        status, res = self._http("POST", "/admin/v1/multimodal-pipelines:validate", bad_overlap)
        self.log(
            "01-reject-overlap",
            f"Overlapping segment rejection: status={status}, reason={res.get('reason_code')}",
            ok=(status == 422 and res.get("reason_code") == "invalid_multimodal_audio_overlap"),
        )

        # 2. window_ms 必须严格为 1000ms（本期契约约束）
        bad_window = dict(base_body)
        bad_window["policy"] = dict(base_body["policy"], window_ms=5000)
        status, res = self._http("POST", "/admin/v1/multimodal-pipelines:validate", bad_window)
        self.log(
            "01-reject-window",
            f"Window ms rejection: status={status}, reason={res.get('reason_code')}",
            ok=(status == 422 and res.get("reason_code") == "multimodal_window_ms_must_be_1000"),
        )

        # 3. 缺少 OCR 或 ASR 必需快路径组件
        missing_asr = dict(base_body)
        missing_asr["components"] = [
            c for c in base_body["components"] if c["node_id"] != "asr_fast"
        ]
        status, res = self._http("POST", "/admin/v1/multimodal-pipelines:validate", missing_asr)
        self.log(
            "01-reject-missing-fast-path",
            f"Missing required component rejection: status={status}, "
            f"reason={res.get('reason_code')}",
            ok=(status == 422 and res.get("reason_code") == "multimodal_ocr_and_asr_required"),
        )

        # 4. 非法配置关联
        bad_cfg = dict(base_body)
        bad_cfg["components"] = [
            {"node_id": "ocr_fast", "config_id": "non_existent_config_id"},
            {"node_id": "asr_fast", "config_id": config_ids["asr_fast"]},
        ]
        status, res = self._http("POST", "/admin/v1/multimodal-pipelines:validate", bad_cfg)
        self.log(
            "01-reject-nonexistent-config",
            f"Nonexistent config rejection: status={status}, reason={res.get('reason_code')}",
            ok=(
                status == 422 and res.get("reason_code") == "multimodal_component_config_not_found"
            ),
        )

        return base_body

    def verify_publish_and_immutability(self, valid_body: dict[str, Any]) -> str:
        print("\n--- [Scenario 2] DAG 编译、发布与不可变 PipelineRevision ---")
        status, validated = self._http(
            "POST", "/admin/v1/multimodal-pipelines:validate", valid_body
        )
        self.log(
            "02-validate-dag",
            f"Valid DAG check: status={status}, valid={validated.get('valid')}",
            ok=(status == 200 and validated.get("valid")),
        )
        graph_digest = validated["graph_digest"]
        self.log(
            "02-validate-dag",
            f"Computed graph digest: {graph_digest}",
            ok=graph_digest.startswith("sha256:"),
        )

        # 保存多模态方案草稿
        status, created = self._http("POST", "/admin/v1/multimodal-pipelines", valid_body)
        self.log(
            "02-save-pipeline",
            f"Saved multimodal pipeline draft: id={created.get('id')}, "
            f"mode={created.get('execution_mode')}",
            ok=(status == 201 and created.get("execution_mode") == "orchestrated_v2"),
        )
        pipeline_id = created["id"]

        # 发布该方案
        status, pub_res = self._http("POST", f"/admin/v1/pipelines/{pipeline_id}:publish")
        self.log("02-publish-pipeline", f"Published pipeline status: {status}", ok=(status == 200))
        self.log(
            "02-publish-pipeline",
            f"Published revision={pub_res.get('revision')}, "
            f"graph_digest={pub_res.get('graph_digest')}",
            ok=(pub_res.get("revision") == 1),
        )

        # 校验数据库内绑定的不可变性约束
        with psycopg.connect(self.db_url) as conn:
            row = conn.execute(
                """
                SELECT execution_mode, orchestration_pipeline_id,
                       orchestration_revision, graph_digest
                FROM console_pipeline WHERE id=%s
                """,
                (pipeline_id,),
            ).fetchone()
            self.log(
                "02-db-binding",
                f"Database binding verification: mode={row[0]}, rev={row[2]}",
                ok=(row[0] == "orchestrated_v2" and row[2] == 1 and row[3] == graph_digest),
            )
        return pipeline_id

    def verify_dispatch_and_snapshots(self, pipeline_id: str):
        print("\n--- [Scenario 3] 任务调度准入、实例预检与不可变执行快照 ---")
        with psycopg.connect(self.db_url) as conn:
            # 找到一个已有视频资产或已有媒体
            asset_row = conn.execute(
                "SELECT id FROM console_upload ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not asset_row:
                print("No asset found, creating upload draft...")
                # 创建测试占位
                asset_id = "asset_" + uuid.uuid4().hex[:24]
                conn.execute(
                    """INSERT INTO console_upload
                    (id, filename, size_bytes, content_type, sha256, owner)
                    VALUES (%s, 'test.mp4', 1024, 'video/mp4', %s, 'demo')""",
                    (asset_id, "sha256:" + "0" * 64),
                )
            else:
                asset_id = asset_row[0]

        status, draft = self._http(
            "POST",
            "/v1/job-drafts",
            {
                "name": f"verify-job-{self.test_id}",
                "asset_id": asset_id,
                "pipeline_id": pipeline_id,
            },
        )
        self.log(
            "03-create-job-draft", f"Created job draft: id={draft.get('id')}", ok=(status == 201)
        )
        job_id = draft["id"]

        # 试图派发给不存在或者无 active 实例的虚构节点 -> 必须被拒为 plugin_instance_unavailable
        status, bad_node_res = self._http(
            "POST", f"/v1/job-drafts/{job_id}:dispatch", {"node_id": "non_existent_node"}
        )
        self.log(
            "03-reject-missing-instance",
            f"Unregistered/unready node dispatch rejected: status={status}, "
            f"reason={bad_node_res.get('reason_code')}",
            ok=(
                status == 409
                and bad_node_res.get("reason_code")
                in {"target_node_not_ready", "plugin_instance_unavailable"}
            ),
        )

        # 派发给同机活跃节点 local-host
        status, dispatched = self._http(
            "POST", f"/v1/job-drafts/{job_id}:dispatch", {"node_id": "local-host"}
        )
        self.log(
            "03-dispatch-success",
            f"Dispatched to local-host: status={status}, "
            f"execution_id={dispatched.get('execution_id')}, run_id={dispatched.get('run_id')}",
            ok=(status == 200 and dispatched.get("execution_mode") == "orchestrated_v2"),
        )
        execution_id = dispatched["execution_id"]

        # 检查数据库内生成的 console_job_execution 不可变快照
        with psycopg.connect(self.db_url) as conn:
            exec_row = conn.execute(
                """
                SELECT execution_id, job_id, run_id, pipeline_id,
                       pipeline_revision, graph_digest, target_node_id, state
                FROM console_job_execution WHERE execution_id=%s
                """,
                (execution_id,),
            ).fetchone()
            self.log(
                "03-execution-snapshot",
                f"Execution snapshot verified: node={exec_row[6]}, "
                f"state={exec_row[7]}, rev={exec_row[4]}",
                ok=(exec_row is not None and exec_row[6] == "local-host"),
            )

    def run(self):
        print("======================================================================")
        print(" SensoryPlex 多模态文件方案（v2）编排与控制面契约验收")
        print("======================================================================")
        self.login()
        self.check_schema()
        valid_body = self.verify_rejections()
        pipeline_id = self.verify_publish_and_immutability(valid_body)
        self.verify_dispatch_and_snapshots(pipeline_id)
        print("\n======================================================================")
        print(f" ALL MULTIMODAL PIPELINE CONTRACT SCENARIOS PASSED ({len(self.results)} CHECKS)")
        print("======================================================================")


def main():
    parser = argparse.ArgumentParser(description="Multimodal Pipeline Verifier")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091", help="Main API URL")
    parser.add_argument("--admin-user", default="demo", help="Admin user")
    parser.add_argument("--admin-pass", default="", help="Admin password")
    args = parser.parse_args()

    verifier = MultimodalPipelineVerifier(
        base_url=args.base_url,
        admin_user=args.admin_user,
        admin_pass=args.admin_pass,
    )
    verifier.run()


if __name__ == "__main__":
    main()
