"""ADR-026 局域网插件 worker 拓扑闭环验收脚本。

在真实集群上验收：
1. 多节点（同机数据面 + 异构局域网节点）身份与短效令牌注册；
2. 认证心跳、排空（draining）与撤销（revoked）状态机流转；
3. 数据本地性硬性约束（共享内存句柄仅限同机数据面，远程节点仅处理观测与文本）；
4. 硬件加速器与操作系统/架构严格预检；
5. 不可变制品部署意图派发、Agent 认领执行、状态上报与 digest 审计回滚；
6. 完整审计记录（Audit Trail）对账。
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

from tools.node_agent import NodeAgentClient, execute_intent  # noqa: E402


class TopologyVerifier:
    def __init__(
        self,
        base_url: str,
        admin_user: str = "demo",
        admin_pass: str = "test-account-password-2026",
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.csrf_token = ""
        self.cookies = ""
        self.run_id = uuid.uuid4().hex[:8]
        self.results = []

    def log(self, section: str, message: str, ok: bool = True):
        status = "OK" if ok else "FAIL"
        print(f"[{status}] [{section}] {message}")
        self.results.append({"section": section, "message": message, "ok": ok})
        if not ok:
            print(f"Verification halted on failure in: {section}", file=sys.stderr)
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
                body = resp.read().decode("utf-8")
                return status, json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(err_body)
            except Exception:
                return e.code, {"detail": err_body}

    def login(self):
        status, data = self._http(
            "POST",
            "/auth/v1/session",
            {"username": self.admin_user, "password": self.admin_pass},
        )
        if status != 200:
            demo_user = os.environ.get("SENSORYPLEX_DEMO_USERNAME", "demo")
            demo_pass = os.environ.get("SENSORYPLEX_DEMO_PASSWORD", "")
            if not demo_pass:
                demo_pw_file = ROOT / ".data/demo-password"
                if demo_pw_file.is_file():
                    demo_pass = demo_pw_file.read_text().strip()
            if demo_pass:
                status, data = self._http(
                    "POST",
                    "/auth/v1/session",
                    {"username": demo_user, "password": demo_pass},
                )
        self.log("01-auth", f"Login status: {status}", ok=(status == 200))
        self.csrf_token = data.get("csrf_token", "")
        self.log("01-auth", f"Authenticated as {data.get('principal')}")

    def verify_scenario_1_registration(self) -> dict[str, Any]:
        """场景 1：多节点（同机数据面 + 异构局域网节点）注册与能力建档。"""
        node_a = f"node-colocated-mac-{self.run_id}"
        node_b = f"node-lan-gpu-{self.run_id}"
        node_c = f"node-lan-edge-{self.run_id}"

        # 节点 A: 同机数据面节点 (Apple Silicon, Metal, CoreML)
        status, tok_a = self._http(
            "POST",
            "/admin/v1/nodes/enrollment-tokens",
            {"node_id": node_a, "expires_in_minutes": 30},
        )
        self.log("02-enroll", f"Issued token for {node_a}", ok=(status == 201))

        caps_a = {
            "platform": "macos",
            "arch": "aarch64",
            "cpu_cores": 10,
            "memory_bytes": 34359738368,
            "unified_memory_bytes": 34359738368,
            "supported_artifacts": ["local_native", "container"],
            "accelerators": [
                {
                    "accelerator": "metal",
                    "platform": "macos",
                    "state": "ACCELERATOR_STATE_AVAILABLE",
                    "runtime_version": "metal4",
                },
                {
                    "accelerator": "coreml",
                    "platform": "macos",
                    "state": "ACCELERATOR_STATE_AVAILABLE",
                    "runtime_version": "3520.5.1",
                },
            ],
            "labels": {"topology": "co-located", "datacenter": "lan-main"},
        }
        client_a = NodeAgentClient(self.base_url, node_a)
        res_a = client_a.enroll(
            tok_a["token"], f"Mac mini M4 ({node_a})", is_co_located=True, capabilities=caps_a
        )
        self.log(
            "02-enroll",
            f"Node {node_a} enrolled: status={res_a.get('status')}",
            ok=res_a.get("success", False),
        )

        # 节点 B: 异构局域网工作站 (Linux x86_64, NVIDIA CUDA)
        status, tok_b = self._http(
            "POST",
            "/admin/v1/nodes/enrollment-tokens",
            {"node_id": node_b, "expires_in_minutes": 30},
        )
        self.log("02-enroll", f"Issued token for {node_b}", ok=(status == 201))

        caps_b = {
            "platform": "linux",
            "arch": "x86_64",
            "cpu_cores": 32,
            "memory_bytes": 68719476736,
            "unified_memory_bytes": 0,
            "supported_artifacts": ["local_native", "container"],
            "accelerators": [
                {
                    "accelerator": "cuda",
                    "platform": "linux",
                    "state": "ACCELERATOR_STATE_AVAILABLE",
                    "runtime_version": "12.4",
                },
            ],
            "labels": {"topology": "lan-worker", "gpu": "RTX-3090Ti"},
        }
        client_b = NodeAgentClient(self.base_url, node_b)
        res_b = client_b.enroll(
            tok_b["token"],
            f"Ubuntu 3090Ti ({node_b})",
            is_co_located=False,
            capabilities=caps_b,
        )
        self.log(
            "02-enroll",
            f"Node {node_b} enrolled: status={res_b.get('status')}",
            ok=res_b.get("success", False),
        )

        # 节点 C: 局域网边缘 CPU 节点 (Linux aarch64, 无 GPU)
        status, tok_c = self._http(
            "POST",
            "/admin/v1/nodes/enrollment-tokens",
            {"node_id": node_c, "expires_in_minutes": 30},
        )
        self.log("02-enroll", f"Issued token for {node_c}", ok=(status == 201))

        caps_c = {
            "platform": "linux",
            "arch": "aarch64",
            "cpu_cores": 4,
            "memory_bytes": 8589934592,
            "unified_memory_bytes": 0,
            "supported_artifacts": ["local_native"],
            "accelerators": [],
            "labels": {"topology": "lan-worker", "device": "edge-box"},
        }
        client_c = NodeAgentClient(self.base_url, node_c)
        res_c = client_c.enroll(
            tok_c["token"], f"Edge ARM CPU ({node_c})", is_co_located=False, capabilities=caps_c
        )
        self.log(
            "02-enroll",
            f"Node {node_c} enrolled: status={res_c.get('status')}",
            ok=res_c.get("success", False),
        )

        status, list_data = self._http("GET", "/admin/v1/nodes")
        node_ids = {n["node_id"] for n in list_data.get("items", [])}
        expected = {node_a, node_b, node_c}
        self.log(
            "02-enroll",
            f"Cluster node inventory includes fresh nodes: {expected.issubset(node_ids)}",
            ok=expected.issubset(node_ids),
        )

        return {
            "node_a": node_a,
            "client_a": client_a,
            "node_b": node_b,
            "client_b": client_b,
            "node_c": node_c,
            "client_c": client_c,
        }

    def verify_scenario_2_heartbeat_and_drain_revoke(self, nodes: dict[str, Any]):
        """场景 2：认证心跳维护、排空（draining）与撤销（revoked）阻断。"""
        node_c = nodes["node_c"]
        client_c = nodes["client_c"]

        hb_res = client_c.heartbeat(available_memory_bytes=4000000000, current_concurrency=0)
        self.log(
            "03-heartbeat",
            f"Heartbeat from {node_c}: {hb_res.get('status')}",
            ok=(hb_res.get("status") == "NODE_STATUS_READY"),
        )

        status, drain_data = self._http("POST", f"/admin/v1/nodes/{node_c}:drain")
        self.log(
            "03-heartbeat",
            f"Drained {node_c}: {drain_data.get('status')}",
            ok=(drain_data.get("status") == "NODE_STATUS_DRAINING"),
        )

        status, pre_drain = self._http(
            "POST",
            f"/admin/v1/nodes/{node_c}/preflight",
            {
                "node_id": node_c,
                "plugin_id": "org.sensoryplex.embed-bge-onnx",
            },
        )
        self.log(
            "03-heartbeat",
            f"Preflight on draining node rejected: {pre_drain.get('reason_code')}",
            ok=(pre_drain.get("reason_code") == "node_draining"),
        )

        status, revoke_data = self._http("POST", f"/admin/v1/nodes/{node_c}:revoke")
        self.log(
            "03-heartbeat",
            f"Revoked {node_c}: {revoke_data.get('status')}",
            ok=(revoke_data.get("status") == "NODE_STATUS_REVOKED"),
        )

        try:
            rev_hb = client_c.heartbeat()
            self.log(
                "03-heartbeat",
                f"Heartbeat after revoke response: {rev_hb.get('status')}",
                ok=(rev_hb.get("status") == "NODE_STATUS_REVOKED"),
            )
        except RuntimeError as e:
            self.log("03-heartbeat", f"Heartbeat rejected with expected error: {e}", ok=True)

    def verify_scenario_3_data_locality(self, nodes: dict[str, Any]):
        """场景 3：数据本地性严格校验（ADR-010 / ADR-026 §2.5）。"""
        node_a = nodes["node_a"]
        node_b = nodes["node_b"]

        status, pre_vlm_remote = self._http(
            "POST",
            f"/admin/v1/nodes/{node_b}/preflight",
            {
                "node_id": node_b,
                "plugin_id": "org.sensoryplex.vlm-moondream",
            },
        )
        self.log(
            "04-locality",
            f"Preflight VLM on remote LAN node rejected: {pre_vlm_remote.get('reason_code')}",
            ok=(pre_vlm_remote.get("reason_code") == "data_locality_violation"),
        )

        status, deploy_fail = self._http(
            "POST",
            f"/admin/v1/nodes/{node_b}/plugins/org.sensoryplex.vlm-moondream:deploy",
            {},
        )
        self.log(
            "04-locality",
            f"Deploy VLM on remote LAN node blocked with HTTP {status}",
            ok=(status == 422 and deploy_fail.get("reason_code") == "data_locality_violation"),
        )

        status, pre_vlm_local = self._http(
            "POST",
            f"/admin/v1/nodes/{node_a}/preflight",
            {
                "node_id": node_a,
                "plugin_id": "org.sensoryplex.vlm-moondream",
            },
        )
        self.log(
            "04-locality",
            f"Preflight VLM on co-located node approved: eligible={pre_vlm_local.get('eligible')}",
            ok=pre_vlm_local.get("eligible", False),
        )

        status, pre_bge_remote = self._http(
            "POST",
            f"/admin/v1/nodes/{node_b}/preflight",
            {
                "node_id": node_b,
                "plugin_id": "org.sensoryplex.embed-bge-onnx",
            },
        )
        self.log(
            "04-locality",
            f"Preflight BGE on remote LAN node approved: eligible={pre_bge_remote.get('eligible')}",
            ok=pre_bge_remote.get("eligible", False),
        )

    def verify_scenario_4_platform_and_accelerator(self, nodes: dict[str, Any]):
        """场景 4：加速器与平台能力精准对账。"""
        node_a = nodes["node_a"]
        node_linux = f"node-colocated-linux-{self.run_id}"

        status, tok = self._http(
            "POST",
            "/admin/v1/nodes/enrollment-tokens",
            {"node_id": node_linux, "expires_in_minutes": 30},
        )
        client_linux = NodeAgentClient(self.base_url, node_linux)
        client_linux.enroll(
            tok["token"],
            f"Colocated Linux ({node_linux})",
            is_co_located=True,
            capabilities={
                "platform": "linux",
                "arch": "x86_64",
                "cpu_cores": 16,
                "memory_bytes": 32000000000,
                "supported_artifacts": ["local_native"],
                "accelerators": [],
            },
        )
        status, pre_mlx_linux = self._http(
            "POST",
            f"/admin/v1/nodes/{node_linux}/preflight",
            {
                "node_id": node_linux,
                "plugin_id": "org.sensoryplex.asr-whisper-mlx",
            },
        )
        self.log(
            "05-accelerator",
            f"Apple Silicon MLX on Linux rejected: {pre_mlx_linux.get('reason_code')}",
            ok=(pre_mlx_linux.get("reason_code") == "unsupported_platform"),
        )

        status, pre_cuda_mac = self._http(
            "POST",
            f"/admin/v1/nodes/{node_a}/preflight",
            {
                "node_id": node_a,
                "plugin_id": "org.sensoryplex.embed-bge-onnx",
                "config": {"provider": "cuda"},
            },
        )
        self.log(
            "05-accelerator",
            f"Requesting CUDA on Metal-only Mac rejected: {pre_cuda_mac.get('reason_code')}",
            ok=(pre_cuda_mac.get("reason_code") == "accelerator_not_available"),
        )

    def verify_scenario_5_deployment_intent_execution_rollback(self, nodes: dict[str, Any]):
        """场景 5：不可变意图派发、Agent 认领、执行上报与版本回滚。"""
        plugin_id = "org.sensoryplex.vlm-moondream"
        node_id = nodes["node_a"]
        client_a = nodes["client_a"]

        # 初始部署
        status, deploy_res = self._http(
            "POST", f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:deploy", {}
        )
        self.log(
            "06-deploy",
            f"Deployed {plugin_id} to {node_id}: state={deploy_res.get('actual_state')}",
            ok=(status == 201 and deploy_res.get("actual_state") == "installing"),
        )

        hb_res = client_a.heartbeat()
        intents = hb_res.get("pending_intents", [])
        self.log("06-deploy", f"Agent claimed {len(intents)} intent(s)", ok=(len(intents) >= 1))

        intent = intents[0]
        exec_ok = execute_intent(intent, client_a)
        self.log("06-deploy", f"Agent executed intent {intent.get('intent_id')}", ok=exec_ok)

        status, node_info = self._http("GET", f"/admin/v1/nodes/{node_id}")
        inst = next(
            (x for x in node_info.get("instances", []) if x["plugin_id"] == plugin_id), None
        )
        self.log(
            "06-deploy",
            f"Plugin instance active state: {inst.get('actual_state') if inst else 'none'}",
            ok=(inst is not None and inst.get("actual_state") == "ready"),
        )

        # 回滚保护：首次部署无 previous_digest，必须 422 拒绝
        status, rb_fail = self._http(
            "POST", f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:rollback"
        )
        self.log(
            "06-deploy",
            f"Rollback without history rejected: {rb_fail.get('reason_code')}",
            ok=(status == 422 and rb_fail.get("reason_code") == "no_previous_digest_for_rollback"),
        )

        # 再次部署升级 -> 记录历史 digest
        self._http("POST", f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:deploy", {})
        status, rb_ok = self._http(
            "POST", f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:rollback"
        )
        self.log(
            "06-deploy",
            f"Rollback with history succeeded: state={rb_ok.get('actual_state')}",
            ok=(status == 200 and rb_ok.get("actual_state") == "rolled_back"),
        )

    def verify_scenario_6_audit_trail(self):
        """场景 6：全生命周期审计事件对账。"""
        status, audit_data = self._http("GET", "/admin/v1/audit-events?limit=100")
        items = audit_data.get("items", [])
        actions = {evt["action"] for evt in items}
        required_actions = {
            "node.token.create",
            "node.enroll.success",
            "node.preflight.pass",
            "node.preflight.reject",
            "node.drain",
            "node.revoke",
            "plugin.instance.deploy",
            "plugin.instance.ready",
        }
        self.log(
            "07-audit",
            f"Recorded audit actions ({len(actions)} distinct)",
            ok=required_actions.issubset(actions),
        )


def main():
    parser = argparse.ArgumentParser(description="Verify ADR-026 Node Topology")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    print("======================================================================")
    print(" SensoryPlex ADR-026 LAN Plugin Worker Topology Verification")
    print("======================================================================")

    password = args.password
    if not password:
        pw_file = ROOT / ".data/demo-password"
        if pw_file.is_file():
            password = pw_file.read_text().strip()
    verifier = TopologyVerifier(args.base_url, args.user, password)
    verifier.login()

    nodes = verifier.verify_scenario_1_registration()
    verifier.verify_scenario_2_heartbeat_and_drain_revoke(nodes)
    verifier.verify_scenario_3_data_locality(nodes)
    verifier.verify_scenario_4_platform_and_accelerator(nodes)
    verifier.verify_scenario_5_deployment_intent_execution_rollback(nodes)
    verifier.verify_scenario_6_audit_trail()

    print("\n======================================================================")
    print(" ALL 6 VERIFICATION SCENARIOS PASSED (ADR-026 IMPLEMENTED)")
    print("======================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
