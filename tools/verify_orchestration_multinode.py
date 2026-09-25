"""ADR-029 P2 受控多节点编排闭环验收脚本。

在真实 PostgreSQL 与容器 API 栈上全量验证：
1. 局域网多节点注册与能力画像（同机数据面、远程 GPU 节点、边缘 CPU 节点）；
2. 插件按节点不可变制品部署与状态对账；
3. 基于数据本地性的多节点任务分发（raw buffer 仅同机，observation 安全跨机派发）；
4. 节点离线/排空硬阻断与防隐式改派（绝不降级至未就绪节点）；
5. 可审计多节点故障转移（Failover：第一任租约过期记账，第二任节点接手完成）；
6. 跨机 Raw Descriptor 边解析期硬拒绝（防数据面越权外发）。
"""

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

from tools.node_agent import NodeAgentClient, execute_intent  # noqa: E402


class MultiNodeOrchestrationVerifier:
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

    def _enroll_node(
        self, node_id: str, display_name: str, is_co_located: bool, caps: dict[str, Any]
    ) -> str:
        tok_res = self._http(
            "POST",
            "/admin/v1/nodes/enrollment-tokens",
            {"node_id": node_id, "expires_in_minutes": 30},
        )[1]
        token = tok_res["token"]
        en_res = self._http(
            "POST",
            "/v1/agent/enroll",
            {
                "enrollment_token": token,
                "node_id": node_id,
                "display_name": display_name,
                "is_co_located": is_co_located,
                "capabilities": caps,
            },
        )[1]
        return en_res["session_token"]

    def run(self):
        print("======================================================================")
        print(" SensoryPlex ADR-029 P2 受控多节点集群编排闭环验收")
        print("======================================================================")

        self.login()

        # 1. 局域网多节点注册与画像校验 (Scenario 1)
        print("\n--- [Scenario 1] 多节点注册与算力画像 ---")
        node_local = f"node-colocated-{self.test_id}"
        node_gpu = f"node-lan-gpu-{self.test_id}"
        node_edge = f"node-lan-edge-{self.test_id}"

        tok_local = self._enroll_node(
            node_local,
            "同机数据面 Mac mini",
            is_co_located=True,
            caps={
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 12,
                "memory_bytes": 32 * 1024**3,
                "unified_memory_bytes": 32 * 1024**3,
                "accelerators": [{"accelerator": "metal", "platform": "macos"}],
                "supported_artifacts": ["local_native"],
            },
        )
        tok_gpu = self._enroll_node(
            node_gpu,
            "局域网 Linux GPU 3090Ti",
            is_co_located=False,
            caps={
                "platform": "linux",
                "arch": "x86_64",
                "cpu_cores": 16,
                "memory_bytes": 64 * 1024**3,
                "accelerators": [{"accelerator": "cuda", "platform": "linux"}],
                "supported_artifacts": ["local_native", "container"],
            },
        )
        tok_edge = self._enroll_node(
            node_edge,
            "局域网 Edge CPU 盒子",
            is_co_located=False,
            caps={
                "platform": "linux",
                "arch": "aarch64",
                "cpu_cores": 4,
                "memory_bytes": 8 * 1024**3,
                "supported_artifacts": ["local_native"],
            },
        )

        status, node_list = self._http("GET", "/admin/v1/nodes")
        node_ids = {n["node_id"] for n in node_list.get("items", [])}
        self.log(
            "01-multi-node-inventory",
            f"Enrolled nodes active in cluster: {len(node_ids)} total",
            ok=({node_local, node_gpu, node_edge}.issubset(node_ids)),
        )

        client_local = NodeAgentClient(self.base_url, node_local, tok_local)
        client_gpu = NodeAgentClient(self.base_url, node_gpu, tok_gpu)
        client_edge = NodeAgentClient(self.base_url, node_edge, tok_edge)

        # 2. 插件按节点部署与不可变制品校验 (Scenario 2)
        print("\n--- [Scenario 2] 插件按节点部署与状态对账 ---")
        p_ocr = "org.sensoryplex.ocr-rapidocr"
        p_embed = "org.sensoryplex.embed-bge-onnx"

        # 在同机节点部署 OCR
        self._http("POST", f"/admin/v1/nodes/{node_local}/plugins/{p_ocr}:deploy", {})
        intents_local = client_local.heartbeat().get("pending_intents", [])
        if intents_local:
            execute_intent(intents_local[0], client_local)

        # 在远程 GPU 节点与 Edge 节点部署 BGE 向量插件
        self._http("POST", f"/admin/v1/nodes/{node_gpu}/plugins/{p_embed}:deploy", {})
        intents_gpu = client_gpu.heartbeat().get("pending_intents", [])
        if intents_gpu:
            execute_intent(intents_gpu[0], client_gpu)

        self._http("POST", f"/admin/v1/nodes/{node_edge}/plugins/{p_embed}:deploy", {})
        intents_edge = client_edge.heartbeat().get("pending_intents", [])
        if intents_edge:
            execute_intent(intents_edge[0], client_edge)

        # 校验各节点插件实例状态均为 ready
        with psycopg.connect(self.db_url) as conn:
            ready_insts = conn.execute(
                """
                SELECT node_id, plugin_id, actual_state
                FROM console_plugin_instance
                WHERE node_id IN (%s, %s, %s) AND actual_state = 'ready'
                """,
                (node_local, node_gpu, node_edge),
            ).fetchall()
        self.log(
            "02-plugin-deployment",
            f"Plugin instances active on designated nodes: {len(ready_insts)} ready",
            ok=(len(ready_insts) == 3),
        )

        # 3. 基于数据本地性的多节点任务调度 (Scenario 3)
        print("\n--- [Scenario 3] 数据本地性调度与跨机 Observation 分发 ---")
        pipeline_id = f"pipe-multinode-{self.test_id}"
        nodes_spec = [
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
                "plugin_id": p_ocr,
                "consumes": ["media.video_frame"],
                "produces": ["observation.ocr_blocks"],
                "placement": "data_plane_local",
                "deadline_ms": 10000,
                "max_attempts": 2,
            },
            {
                "id": "embedding",
                "plugin_id": p_embed,
                "consumes": ["observation.ocr_blocks"],
                "produces": ["observation.text_embedding"],
                "placement": "object_ref_allowed",
                "deadline_ms": 15000,
                "max_attempts": 2,
            },
        ]
        edges_spec = [
            {
                "from": "source",
                "to": "ocr",
                "modality": "media.video_frame",
                "join": "same_item",
                "required": True,
            },
            {
                "from": "ocr",
                "to": "embedding",
                "modality": "observation.ocr_blocks",
                "join": "same_stream_window",
                "required": True,
            },
        ]

        status, pub_res = self._http(
            "POST",
            "/v1/orchestration/pipelines",
            {
                "pipeline_id": pipeline_id,
                "nodes": nodes_spec,
                "edges": edges_spec,
            },
        )
        self.log(
            "03-pipeline-publish",
            f"Multi-node pipeline published: rev={pub_res.get('revision', {}).get('revision')}",
            ok=(status == 200),
        )

        # 提交 Run
        status, sub_res = self._http(
            "POST",
            "/v1/orchestration/runs",
            {
                "pipeline_id": pipeline_id,
                "revision": 1,
                "input_ref": "video://stream-sample.mp4",
                "idempotency_key": f"idemp-mn-{self.test_id}",
            },
        )
        run_id = sub_res["run"]["run_id"]

        # 3.1 远程 GPU 节点尝试认领 raw buffer source 任务 -> 必须被数据本地性守卫拒绝！
        status, claim_gpu_fail = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {
                "node_id": node_gpu,
                "supported_plugins": ["runtime.file-source", p_ocr, p_embed],
                "max_tasks": 1,
            },
        )
        self.log(
            "03-locality-guard",
            f"Remote node claim on data_plane_local task rejected: "
            f"count={len(claim_gpu_fail.get('tasks', []))}",
            ok=(len(claim_gpu_fail.get("tasks", [])) == 0),
        )

        # 3.2 同机数据面节点认领 source 并执行完成
        status, claim_local = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_local, "supported_plugins": ["runtime.file-source", p_ocr]},
        )
        source_claimed = next(
            (t for t in claim_local.get("tasks", []) if t["node_id"] == "source"), None
        )
        asgn_source = next(
            (
                a
                for a in claim_local.get("assignments", [])
                if a["task_id"] == source_claimed["task_id"]
            ),
            None,
        )
        self.log(
            "03-local-source-claim",
            f"Co-located node successfully claimed source: asgn_id={asgn_source['assignment_id']}",
            ok=(source_claimed is not None and asgn_source["actual_node_id"] == node_local),
        )

        self._http(
            "POST",
            f"/v1/orchestration/tasks/{source_claimed['task_id']}:result",
            {
                "run_id": run_id,
                "attempt": 1,
                "assignment_id": asgn_source["assignment_id"],
                "success": True,
                "output_ref": "buffer://descriptor-shm-01",
            },
        )

        # 3.3 同机节点继续认领 ocr 并执行完成 -> 解锁 embedding
        claim_local2 = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_local, "supported_plugins": [p_ocr]},
        )[1]
        ocr_claimed = next(t for t in claim_local2["tasks"] if t["node_id"] == "ocr")
        asgn_ocr = next(
            a for a in claim_local2["assignments"] if a["task_id"] == ocr_claimed["task_id"]
        )
        self._http(
            "POST",
            f"/v1/orchestration/tasks/{ocr_claimed['task_id']}:result",
            {
                "run_id": run_id,
                "attempt": 1,
                "assignment_id": asgn_ocr["assignment_id"],
                "success": True,
                "output_ref": "obs://ocr-blocks-01",
            },
        )

        # 3.4 远程 GPU 节点认领 embedding (object_ref_allowed) -> 成功跨机派发并执行！
        claim_gpu_ok = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_gpu, "supported_plugins": [p_embed]},
        )[1]
        emb_claimed = next(
            (t for t in claim_gpu_ok.get("tasks", []) if t["node_id"] == "embedding"), None
        )
        asgn_emb = next(
            (
                a
                for a in claim_gpu_ok.get("assignments", [])
                if a["task_id"] == emb_claimed["task_id"]
            ),
            None,
        )
        self.log(
            "03-remote-embedding-claim",
            f"Observation task successfully distributed to remote GPU node: "
            f"actual_node={asgn_emb.get('actual_node_id')}",
            ok=(emb_claimed is not None and asgn_emb.get("actual_node_id") == node_gpu),
        )

        # 远程 GPU 节点执行并上报最终结果
        rep_emb = self._http(
            "POST",
            f"/v1/orchestration/tasks/{emb_claimed['task_id']}:result",
            {
                "run_id": run_id,
                "attempt": 1,
                "assignment_id": asgn_emb["assignment_id"],
                "success": True,
                "output_ref": "obs://embed-vec-01",
            },
        )[1]
        self.log(
            "03-multi-node-run-completed",
            f"Multi-node pipeline run finished: run_completed={rep_emb.get('run_completed')}",
            ok=(rep_emb.get("run_completed") is True),
        )

        # 4. 节点离线阻断与防隐式改派 (Scenario 4)
        print("\n--- [Scenario 4] 节点排空/离线硬阻断 ---")
        # 将 GPU 节点置为 draining
        self._http("POST", f"/admin/v1/nodes/{node_gpu}:drain")
        status, pre_draining = self._http(
            "POST",
            f"/admin/v1/nodes/{node_gpu}/preflight",
            {"node_id": node_gpu, "plugin_id": p_embed},
        )
        self.log(
            "04-draining-node-rejected",
            f"Preflight on draining node rejected: {pre_draining.get('reason_code')}",
            ok=(
                pre_draining.get("eligible") is False
                and pre_draining.get("reason_code") == "node_draining"
            ),
        )

        # 尝试由 draining 节点认领任务 -> 409 显式拒绝
        status, claim_draining = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_gpu, "supported_plugins": [p_embed]},
        )
        self.log(
            "04-draining-claim-blocked",
            f"Claim on draining node rejected: HTTP {status} {claim_draining.get('detail')}",
            ok=(status == 409 and "node_not_eligible" in claim_draining.get("detail", "")),
        )

        # 5. 可审计多节点故障转移 (Scenario 5)
        print("\n--- [Scenario 5] 故障转移 (Failover) 与全周期审计对账 ---")
        # 恢复 GPU 节点为 ready
        with psycopg.connect(self.db_url) as conn:
            conn.execute("UPDATE console_node SET status='ready' WHERE node_id=%s", (node_gpu,))

        status, sub_failover = self._http(
            "POST",
            "/v1/orchestration/runs",
            {
                "pipeline_id": pipeline_id,
                "revision": 1,
                "input_ref": "video://failover.mp4",
                "idempotency_key": f"idemp-fo-{self.test_id}",
            },
        )
        fo_run_id = sub_failover["run"]["run_id"]
        fo_tasks = {t["node_id"]: t for t in sub_failover["tasks"]}

        # 完成 source 与 ocr
        c_src = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_local, "supported_plugins": ["runtime.file-source"]},
        )[1]["assignments"][0]
        self._http(
            "POST",
            f"/v1/orchestration/tasks/{fo_tasks['source']['task_id']}:result",
            {
                "run_id": fo_run_id,
                "attempt": 1,
                "assignment_id": c_src["assignment_id"],
                "success": True,
                "output_ref": "buf://1",
            },
        )
        c_ocr = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_local, "supported_plugins": [p_ocr]},
        )[1]["assignments"][0]
        self._http(
            "POST",
            f"/v1/orchestration/tasks/{fo_tasks['ocr']['task_id']}:result",
            {
                "run_id": fo_run_id,
                "attempt": 1,
                "assignment_id": c_ocr["assignment_id"],
                "success": True,
                "output_ref": "obs://1",
            },
        )

        # 节点 1 (node_gpu) 认领 embedding 任务 (attempt 1)
        c_emb1 = self._http(
            "POST",
            "/v1/orchestration/tasks:claim",
            {"node_id": node_gpu, "supported_plugins": [p_embed]},
        )[1]["assignments"][0]
        self.log(
            "05-failover-initial-dispatch",
            f"Embedding task dispatched to node_gpu: asgn_id={c_emb1['assignment_id']}",
            ok=(c_emb1["actual_node_id"] == node_gpu and c_emb1["attempt"] == 1),
        )

        # 模拟 node_gpu 掉线与租约超时崩溃
        with psycopg.connect(self.db_url) as conn:
            conn.execute(
                """
                UPDATE scheduler_assignment
                SET lease_expires_at = now() - interval '10 seconds'
                WHERE assignment_id = %s
                """,
                (c_emb1["assignment_id"],),
            )
            conn.execute("UPDATE console_node SET status='offline' WHERE node_id=%s", (node_gpu,))

        # 触发恢复器与指定备用节点 (node_edge) 重新调度 (Failover)
        sched_step = self._http(
            "POST",
            "/v1/orchestration/scheduler:step",
            {"node_id": node_edge, "is_co_located": False, "supported_plugins": [p_embed]},
        )[1]
        rec = sched_step.get("recovery", {})
        self.log(
            "05-failover-detected",
            f"Reconciler detected failed node and reclaimed task: "
            f"recovered={rec.get('recovered_tasks')}",
            ok=(fo_tasks["embedding"]["task_id"] in rec.get("recovered_tasks", [])),
        )

        # 验证任务成功 failover 改派至备用节点 node_edge，且 attempt 递增为 2
        assigned_list = sched_step.get("assigned_tasks", [])
        asgn_fo2 = next(
            (a for a in assigned_list if a["task_id"] == fo_tasks["embedding"]["task_id"]), None
        )
        self.log(
            "05-failover-reassigned",
            f"Task cleanly failed over to backup node_edge: "
            f"actual_node={asgn_fo2.get('actual_node_id') if asgn_fo2 else None}, "
            f"attempt={asgn_fo2.get('attempt') if asgn_fo2 else None}",
            ok=(
                asgn_fo2 is not None
                and asgn_fo2.get("actual_node_id") == node_edge
                and asgn_fo2.get("attempt") == 2
            ),
        )

        # 检查持久化调度账目中同时保留了这两次 assignment 记录 (审计对账)
        with psycopg.connect(self.db_url) as conn:
            all_asgns = conn.execute(
                """
                SELECT assignment_id, actual_node_id, attempt, decision
                FROM scheduler_assignment
                WHERE task_id = %s
                ORDER BY attempt ASC
                """,
                (fo_tasks["embedding"]["task_id"],),
            ).fetchall()
        self.log(
            "05-failover-audit-trail",
            f"Complete assignment audit trail retained: "
            f"count={len(all_asgns)}, decisions={[a[3] for a in all_asgns]}",
            ok=(
                len(all_asgns) == 2
                and all_asgns[0][1] == node_gpu
                and all_asgns[0][3] in ("lease_expired", "node_offline")
                and all_asgns[1][1] == node_edge
                and all_asgns[1][3] == "assigned"
            ),
        )

        # 6. 跨机 Raw Descriptor 边解析期硬拒绝 (Scenario 6)
        print("\n--- [Scenario 6] 跨机 Raw Descriptor 边解析期拒绝 ---")
        invalid_cross_pipe = [
            {
                "id": "raw_source",
                "plugin_id": "runtime.file-source",
                "consumes": [],
                "produces": ["media.video_frame"],
                "placement": "data_plane_local",
                "deadline_ms": 1000,
            },
            {
                "id": "remote_consumer",
                "plugin_id": "remote.processor",
                "consumes": ["media.video_frame"],
                "produces": ["obs.result"],
                "placement": "object_ref_allowed",  # 跨机节点
                "deadline_ms": 1000,
            },
        ]
        invalid_cross_edges = [
            {
                "from": "raw_source",
                "to": "remote_consumer",
                "modality": "media.video_frame",
                "join": "same_item",  # same_item 试图跨越至非 data_plane_local 节点
            }
        ]
        status, cross_res = self._http(
            "POST",
            "/v1/orchestration/pipelines:validate",
            {"nodes": invalid_cross_pipe, "edges": invalid_cross_edges},
        )
        self.log(
            "06-cross-node-descriptor-rejection",
            f"Cross-node raw buffer edge rejected in compiler: "
            f"valid={cross_res.get('valid')}, errors={cross_res.get('errors')}",
            ok=(
                cross_res.get("valid") is False
                and "same_item_requires_data_plane_local" in cross_res.get("errors", [])
            ),
        )

        print("\n======================================================================")
        print(" ALL 6 MULTI-NODE ORCHESTRATION SCENARIOS PASSED (ADR-029 P2 CLOSED)")
        print("======================================================================")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Verify ADR-029 P2 Multi-Node Orchestration")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    verifier = MultiNodeOrchestrationVerifier(args.base_url, args.user, args.password)
    return verifier.run()


if __name__ == "__main__":
    sys.exit(main())
