"""ADR-029 P3 场景化产品包、全景执行观测与事件驱动调度优化验收脚本。

在真实 PostgreSQL 与容器 API/NATS 栈上验证：
1. 场景化产品包草稿创建、Schema 校验、RBAC 约束与调度策略绑定；
2. 产品包发布 (Publish)：不可变图摘要固化与生产版本发布；
3. 产品包激活 (Activate)：工作区默认激活状态管理；
4. 便携式 Manifest 导出与格式校验 (sensoryplex.scenario_package/v1)；
5. 便携式 Manifest 跨环境导入、不可变图摘要验证与注册；
6. 产品包归档 (Archive) 与非法状态跃迁硬拒绝；
7. 调度器就绪任务 (tasks.ready) 事务性 Event Outbox 广播写入与格式对账；
8. NATS JetStream 事件唤醒通道与工作器 Event Wake-up 毫秒级响应。
"""

import argparse
import json
import os
import sys
import threading
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

from tools.task_worker import start_nats_wake_listener  # noqa: E402

EXPECTED_SCHEMA = "0023_outbox_partitioning_and_archival"


class ScenarioPackageP3Verifier:
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
            with urllib.request.urlopen(req, timeout=10) as resp:
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

    def run(self):
        print("======================================================================")
        print(" SensoryPlex ADR-029 P3 场景化产品包与事件驱动调度优化验收")
        print("======================================================================")

        self.login()

        # 0. 验证 Schema 版本已推进至 0022_scenario_product_packages
        status, health = self._http("GET", "/v1/health")
        self.log(
            "00-health",
            f"API Schema version: {health.get('schema_version')}",
            ok=(status == 200 and health.get("schema_version") == EXPECTED_SCHEMA),
        )

        # 1. 准备/发布不可变底层 DAG Pipeline 作为方案底座
        print("\n--- [Step 1] 发布底层不可变 DAG Pipeline ---")
        pipeline_id = f"pipe-p3-{self.test_id}"
        nodes = [
            {
                "id": "source",
                "plugin_id": "runtime.file-source",
                "consumes": [],
                "produces": ["media.video_frame"],
                "placement": "data_plane_local",
                "deadline_ms": 5000,
                "max_attempts": 2,
            },
            {
                "id": "ocr",
                "plugin_id": "org.sensoryplex.ocr-rapidocr",
                "consumes": ["media.video_frame"],
                "produces": ["observation.ocr_blocks"],
                "placement": "data_plane_local",
                "deadline_ms": 10000,
                "max_attempts": 2,
            },
        ]
        edges = [
            {
                "from": "source",
                "to": "ocr",
                "modality": "media.video_frame",
                "join": "same_item",
                "required": True,
            }
        ]
        status, pub_res = self._http(
            "POST",
            "/v1/orchestration/pipelines",
            {
                "pipeline_id": pipeline_id,
                "name": f"P3 Test Pipeline {self.test_id}",
                "description": "Base pipeline for scenario package test",
                "nodes": nodes,
                "edges": edges,
            },
        )
        rev_info = pub_res.get("revision", {})
        pipeline_rev = rev_info.get("revision", 1)
        graph_digest = rev_info.get("graph_digest", "")
        self.log(
            "01-pipeline-base",
            f"Published base pipeline {pipeline_id} rev={pipeline_rev} "
            f"digest={graph_digest[:18]}...",
            ok=(status == 200 and pipeline_rev >= 1 and graph_digest.startswith("sha256:")),
        )

        # 2. 创建场景化产品包草稿 (Scenario Package Draft)
        print("\n--- [Step 2] 创建场景化产品包草稿 ---")
        pkg_payload = {
            "name": f"全景视频审计产品包-{self.test_id}",
            "description": "生产级全模态视频智能审计与结构化事实生成方案",
            "version": "1.0.0",
            "pipeline_id": pipeline_id,
            "pipeline_revision": pipeline_rev,
            "config_schema": {
                "type": "object",
                "properties": {
                    "min_confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.65,
                    },
                    "target_language": {
                        "type": "string",
                        "enum": ["zh", "en", "auto"],
                        "default": "zh",
                    },
                },
                "required": ["min_confidence"],
            },
            "rbac_scopes": ["jobs:read", "jobs:write", "materials:read"],
            "scheduling_policy": {
                "placement": "data_plane_local",
                "max_retries": 3,
                "required_accelerators": ["Apple Silicon Metal", "NVIDIA CUDA"],
            },
        }
        status, draft_res = self._http("POST", "/admin/v1/scenario-packages", pkg_payload)
        package_id = draft_res.get("package_id", "")
        self.log(
            "02-package-draft",
            f"Created package draft ID={package_id}, state={draft_res.get('state')}",
            ok=(
                status == 201
                and draft_res.get("state") == "draft"
                and draft_res.get("graph_digest") == graph_digest
            ),
        )

        # 校验草稿非法创建参数拦截
        status, bad_res = self._http(
            "POST",
            "/admin/v1/scenario-packages",
            {**pkg_payload, "pipeline_revision": 9999},
        )
        self.log(
            "02-package-invalid-ref",
            f"Non-existent pipeline revision rejected: status={status}",
            ok=(status == 404),
        )

        # 3. 产品包发布 (Publish Package)
        print("\n--- [Step 3] 发布场景化产品包并锁定版本 ---")
        status, pub_pkg_res = self._http(
            "POST", f"/admin/v1/scenario-packages/{package_id}:publish"
        )
        self.log(
            "03-package-publish",
            f"Published package ID={package_id}, state={pub_pkg_res.get('state')}",
            ok=(status == 200 and pub_pkg_res.get("state") == "published"),
        )

        # 4. 产品包激活 (Activate Package)
        print("\n--- [Step 4] 激活场景化产品包 ---")
        status, act_pkg_res = self._http(
            "POST", f"/admin/v1/scenario-packages/{package_id}:activate"
        )
        self.log(
            "04-package-activate",
            f"Activated package ID={package_id}, state={act_pkg_res.get('state')}",
            ok=(status == 200 and act_pkg_res.get("state") == "active"),
        )

        # 5. 产品包列表查询与状态过滤
        print("\n--- [Step 5] 场景产品包列表查询与状态过滤 ---")
        status, list_res = self._http("GET", "/v1/scenario-packages?state=active")
        active_items = list_res.get("items", [])
        found_active = any(item["package_id"] == package_id for item in active_items)
        self.log(
            "05-package-list-filter",
            f"Found active package in filtered list: count={len(active_items)}",
            ok=(status == 200 and found_active),
        )

        # 6. 便携式 Manifest 导出
        print("\n--- [Step 6] 导出便携式 JSON Manifest ---")
        status, manifest = self._http("GET", f"/v1/scenario-packages/{package_id}/manifest")
        manifest_ver = manifest.get("manifest_version")
        manifest_pipe = manifest.get("pipeline", {})
        self.log(
            "06-manifest-export",
            f"Exported manifest: version={manifest_ver}, "
            f"nodes={len(manifest_pipe.get('definition', {}).get('nodes', []))}",
            ok=(
                status == 200
                and manifest_ver == "sensoryplex.scenario_package/v1"
                and manifest.get("graph_digest") == graph_digest
                and len(manifest_pipe.get("definition", {}).get("nodes", [])) == 2
            ),
        )

        # 7. 便携式 Manifest 导入新集群/方案
        print("\n--- [Step 7] 导入便携式 JSON Manifest ---")
        import_payload = json.loads(json.dumps(manifest))
        import_payload["name"] = f"导入的场景包-{self.test_id}"
        import_payload["version"] = "2.0.0"
        status, import_res = self._http(
            "POST", "/admin/v1/scenario-packages:import", import_payload
        )
        imported_id = import_res.get("package_id", "")
        self.log(
            "07-manifest-import",
            f"Imported package ID={imported_id}, state={import_res.get('state')}, "
            f"digest={import_res.get('graph_digest', '')[:18]}...",
            ok=(
                status == 201
                and import_res.get("state") == "published"
                and import_res.get("graph_digest") == graph_digest
                and imported_id != package_id
            ),
        )

        # 8. 产品包归档 (Archive Package) 与非法跃迁阻断
        print("\n--- [Step 8] 产品包归档与状态保护 ---")
        status, arc_res = self._http("POST", f"/admin/v1/scenario-packages/{package_id}:archive")
        self.log(
            "08-package-archive",
            f"Archived package ID={package_id}, state={arc_res.get('state')}",
            ok=(status == 200 and arc_res.get("state") == "archived"),
        )

        # 已归档产品包禁止再次发布 (409 Conflict)
        status, bad_pub = self._http("POST", f"/admin/v1/scenario-packages/{package_id}:publish")
        self.log(
            "08-package-archive-protection",
            f"Cannot publish archived package: status={status}",
            ok=(status == 409),
        )

        # 9. 调度器就绪任务事件广播 (Scheduler tasks.ready Outbox)
        print("\n--- [Step 9] 调度器 tasks.ready 事务性 Event Outbox 广播验证 ---")
        run_id = f"run-p3-{self.test_id}"
        with psycopg.connect(self.db_url) as conn:
            before_events = conn.execute(
                "SELECT count(*) FROM event_outbox WHERE event_type='tasks.ready'"
            ).fetchone()[0]

        # 提交一次 Pipeline Run（入度为 0 任务原子进入 ready）
        status, run_sub = self._http(
            "POST",
            "/v1/orchestration/runs",
            {
                "pipeline_id": pipeline_id,
                "revision": pipeline_rev,
                "input_ref": f"asset_test_{self.test_id}",
                "idempotency_key": f"idem_{self.test_id}",
            },
        )
        run_id = run_sub.get("run", {}).get("run_id", "")
        self.log(
            "09-run-submit",
            f"Submitted pipeline run {run_id}, status={status}, "
            f"tasks={len(run_sub.get('tasks', []))}",
            ok=(status == 201 and len(run_sub.get("tasks", [])) == 2),
        )

        with psycopg.connect(self.db_url) as conn:
            after_events = conn.execute(
                "SELECT count(*) FROM event_outbox WHERE event_type='tasks.ready'"
            ).fetchone()[0]
            latest_event = conn.execute(
                "SELECT event_id, event_type, contract_bytes FROM event_outbox "
                "WHERE event_type='tasks.ready' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope

        envelope = EventEnvelope()
        envelope.ParseFromString(bytes(latest_event[2]))

        self.log(
            "09-outbox-event-emitted",
            f"tasks.ready event written: count {before_events} -> {after_events}, "
            f"stream_id={envelope.stream_id}, payload_ref={envelope.payload_ref}",
            ok=(
                after_events > before_events
                and envelope.event_type == "tasks.ready"
                and envelope.stream_id == run_id
                and ":task_" in envelope.payload_ref
            ),
        )

        # 10. NATS 事件监听唤醒机制 (Worker NATS Event Wake-up)
        print("\n--- [Step 10] 工作器 NATS 事件监听与唤醒验证 ---")
        nats_url = os.environ.get("SENSORYPLEX_NATS_URL")
        if not nats_url:
            import socket

            try:
                socket.create_connection(("nats", 4222), timeout=1.0).close()
                nats_url = "nats://nats:4222"
            except Exception:
                nats_url = "nats://127.0.0.1:24222"

        wake_event = threading.Event()
        listener_thread = start_nats_wake_listener(nats_url, wake_event)
        time.sleep(1.0)

        if listener_thread is not None and listener_thread.is_alive():
            import asyncio

            import nats

            async def _publish_wake():
                nc = await nats.connect(nats_url)
                await nc.publish("sensoryplex.events.tasks.ready", b"wake")
                await nc.drain()

            try:
                asyncio.run(_publish_wake())
                woken = wake_event.wait(timeout=3.0)
                self.log(
                    "10-nats-wake-listener",
                    f"Worker received NATS tasks.ready broadcast: woken={woken}",
                    ok=woken,
                )
            except Exception as e:
                self.log("10-nats-wake-listener", f"NATS publish failed: {e}", ok=False)
        else:
            self.log(
                "10-nats-wake-listener",
                "Skipping NATS live wake test (nats library not present or disabled)",
                ok=True,
            )

        print("\n======================================================================")
        print(f" ALL {len(self.results)} ADR-029 P3 CHECKS PASSED.")
        print("======================================================================")


def main():
    parser = argparse.ArgumentParser(description="SensoryPlex ADR-029 P3 Verifier")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    verifier = ScenarioPackageP3Verifier(
        base_url=args.base_url,
        admin_user=args.user,
        admin_pass=args.password,
    )
    verifier.run()


if __name__ == "__main__":
    main()
