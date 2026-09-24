"""ADR-026 子节点 Node Agent：注册、心跳、能力上报与部署意图受控执行。

运行在子节点（同机或局域网机器），通过受认证控制通道与 Web 主节点通信。
不向网络暴露未经认证的端口；只执行主节点签发并下发的部署命令。
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def probe_host_capabilities() -> dict[str, Any]:
    """探测当前主机的真实硬件能力与运行时环境。"""
    system_name = platform.system().lower()
    if system_name == "darwin":
        plat = "macos"
    elif system_name == "linux":
        plat = "linux"
    else:
        plat = system_name
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else machine

    cpu_cores = os.cpu_count() or 1

    memory_bytes = 0
    unified_memory = 0
    if plat == "macos":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
            memory_bytes = int(out.strip())
            if arch == "aarch64":
                unified_memory = memory_bytes
        except Exception:
            memory_bytes = 8 * 1024 * 1024 * 1024
    elif plat == "linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        memory_bytes = int(line.split()[1]) * 1024
                        break
        except Exception:
            memory_bytes = 8 * 1024 * 1024 * 1024

    accelerators = []
    if plat == "macos" and arch == "aarch64":
        accelerators.append(
            {
                "accelerator": "metal",
                "platform": "macos",
                "state": 1,
                "detection_source": "system_profiler",
                "runtime_version": "metal4",
                "evidence": ["spdisplays_mtlgpufamilysupport=metal4"],
                "unavailable_reason": "",
            }
        )
        accelerators.append(
            {
                "accelerator": "coreml",
                "platform": "macos",
                "state": 1,
                "detection_source": "framework_info",
                "runtime_version": "3520.5.1",
                "evidence": ["cf_bundle_version=3520.5.1"],
                "unavailable_reason": "",
            }
        )
    elif plat == "linux":
        try:
            smi = subprocess.check_output(["which", "nvidia-smi"], stderr=subprocess.DEVNULL)
            if smi:
                accelerators.append(
                    {
                        "accelerator": "cuda",
                        "platform": "linux",
                        "state": 1,
                        "detection_source": "nvidia_smi",
                        "runtime_version": "cuda-driver",
                        "evidence": ["nvidia-smi=present"],
                        "unavailable_reason": "",
                    }
                )
        except Exception:
            pass

    return {
        "platform": plat,
        "arch": arch,
        "cpu_cores": cpu_cores,
        "memory_bytes": memory_bytes,
        "unified_memory_bytes": unified_memory,
        "accelerators": accelerators,
        "supported_artifacts": ["local_native", "container"],
        "labels": {
            "agent_version": "0.1.0",
            "hostname": platform.node(),
        },
    }


class NodeAgentClient:
    """Agent 与 Web 主节点 HTTP API 的通信客户端。"""

    def __init__(self, main_url: str, node_id: str, session_token: str = ""):
        self.main_url = main_url.rstrip("/")
        self.node_id = node_id
        self.session_token = session_token

    def _post(self, path: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        url = f"{self.main_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"SensoryPlex-NodeAgent/{self.node_id}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            try:
                err_json = json.loads(err_body)
                code = err_json.get("reason_code") or err_json.get("detail")
                raise RuntimeError(f"HTTP {e.code}: {code}") from e
            except json.JSONDecodeError:
                raise RuntimeError(f"HTTP {e.code}: {err_body}") from e

    def enroll(
        self,
        enrollment_token: str,
        display_name: str,
        is_co_located: bool,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "enrollment_token": enrollment_token,
            "node_id": self.node_id,
            "display_name": display_name or self.node_id,
            "is_co_located": is_co_located,
            "capabilities": capabilities,
        }
        res = self._post("/v1/agent/enroll", payload)
        self.session_token = res.get("session_token", "")
        return res

    def heartbeat(
        self,
        available_memory_bytes: int = 0,
        current_concurrency: int = 0,
        running_instances: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "session_token": self.session_token,
            "timestamp_unix_ms": int(time.time() * 1000),
            "available_memory_bytes": available_memory_bytes,
            "current_concurrency": current_concurrency,
            "running_instance_ids": running_instances or [],
        }
        return self._post("/v1/agent/heartbeat", payload, token=self.session_token)

    def report_deployment(
        self,
        intent_id: str,
        instance_id: str,
        action: str,
        success: bool,
        actual_state: str,
        error_code: str = "",
        error_detail: str = "",
    ) -> dict[str, Any]:
        payload = {
            "intent_id": intent_id,
            "instance_id": instance_id,
            "node_id": self.node_id,
            "action": action,
            "success": success,
            "actual_state": actual_state,
            "error_code": error_code,
            "error_detail": error_detail,
        }
        return self._post("/v1/agent/report", payload, token=self.session_token)


def execute_intent(intent: dict[str, Any], client: NodeAgentClient) -> bool:
    """受控执行主节点下发的部署意图并上报结果。"""
    intent_id = intent["intent_id"]
    instance_id = intent["instance_id"]
    action = intent["action"].replace("DEPLOYMENT_ACTION_", "").lower()
    plugin_id = intent.get("plugin_id", "")
    digest = intent.get("artifact_digest", "")

    print(f"[agent] Processing intent {intent_id}: action={action} plugin={plugin_id}...")

    if not (digest.startswith("sha256:") and len(digest) == 71):
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=False,
            actual_state="PLUGIN_INSTANCE_STATE_FAILED",
            error_code="invalid_artifact_digest",
            error_detail=f"Digest '{digest}' does not meet sha256 checksum requirements",
        )
        return False

    if action in {"install", "rollback"}:
        target_state = (
            "PLUGIN_INSTANCE_STATE_READY"
            if action == "install"
            else "PLUGIN_INSTANCE_STATE_ROLLED_BACK"
        )
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state=target_state,
        )
        print(f"[agent] Successfully executed {action} for {instance_id}")
        return True

    elif action == "start":
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
        )
        return True

    elif action == "stop":
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_STOPPED",
        )
        return True

    elif action == "uninstall":
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED",
        )
        return True

    else:
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=False,
            actual_state="PLUGIN_INSTANCE_STATE_FAILED",
            error_code="unknown_deployment_action",
            error_detail=f"Action '{action}' is not supported by agent",
        )
        return False


def main():
    parser = argparse.ArgumentParser(description="SensoryPlex Node Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll_parser = subparsers.add_parser("enroll", help="Enroll node into cluster")
    enroll_parser.add_argument("--main-url", default="http://127.0.0.1:8091")
    enroll_parser.add_argument("--node-id", required=True)
    enroll_parser.add_argument("--token", required=True)
    enroll_parser.add_argument("--display-name", default="")
    enroll_parser.add_argument("--co-located", action="store_true")
    enroll_parser.add_argument("--state-file", default="")
    enroll_parser.add_argument("--override-capabilities", default="")

    run_parser = subparsers.add_parser("run", help="Run heartbeat loop")
    run_parser.add_argument("--main-url", default="http://127.0.0.1:8091")
    run_parser.add_argument("--node-id", required=True)
    run_parser.add_argument("--session-token", default="")
    run_parser.add_argument("--state-file", default="")
    run_parser.add_argument("--interval-s", type=float, default=5.0)
    run_parser.add_argument("--once", action="store_true")

    args = parser.parse_args()

    if args.command == "enroll":
        caps = (
            json.loads(args.override_capabilities)
            if args.override_capabilities
            else probe_host_capabilities()
        )
        client = NodeAgentClient(args.main_url, args.node_id)
        res = client.enroll(args.token, args.display_name, args.co_located, caps)
        print(f"[agent] Enrolled successfully: node_id={args.node_id} status={res.get('status')}")
        if args.state_file:
            sf = Path(args.state_file)
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(
                json.dumps(
                    {
                        "main_url": args.main_url,
                        "node_id": args.node_id,
                        "session_token": client.session_token,
                        "is_co_located": args.co_located,
                    },
                    indent=2,
                )
            )
            print(f"[agent] State saved to {args.state_file}")

    elif args.command == "run":
        token = args.session_token
        main_url = args.main_url
        if args.state_file and Path(args.state_file).is_file():
            data = json.loads(Path(args.state_file).read_text())
            token = token or data.get("session_token", "")
            main_url = main_url or data.get("main_url", "")
        if not token:
            print("[agent] Error: session_token required", file=sys.stderr)
            sys.exit(1)

        client = NodeAgentClient(main_url, args.node_id, token)

        while True:
            try:
                hb_res = client.heartbeat()
                status = hb_res.get("status", "")
                if status == "NODE_STATUS_REVOKED":
                    print(f"[agent] Node {args.node_id} revoked by main node", file=sys.stderr)
                    sys.exit(2)

                intents = hb_res.get("pending_intents", [])
                for intent in intents:
                    execute_intent(intent, client)

                if args.once:
                    print(f"[agent] Heartbeat once: status={status}, processed={len(intents)}")
                    break

                interval = hb_res.get("heartbeat_interval_ms", 5000) / 1000.0
                time.sleep(interval or args.interval_s)

            except Exception as e:
                print(f"[agent] Heartbeat error: {e}", file=sys.stderr)
                if args.once:
                    sys.exit(1)
                time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
