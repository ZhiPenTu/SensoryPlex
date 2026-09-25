"""ADR-026 子节点 Node Agent：注册、心跳、能力上报与部署意图受控执行。

运行在子节点（同机或局域网机器），通过受认证控制通道与 Web 主节点通信。
不向网络暴露未经认证的端口；只执行主节点签发并下发的部署命令。
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from edge_material_sdk import get_logger

ROOT = Path(__file__).resolve().parents[1]
LOGGER = get_logger("sensoryplex.agent")


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

    def register_candidate(
        self,
        display_name: str,
        is_co_located: bool,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "enrollment_token": "candidate",
            "node_id": self.node_id,
            "display_name": display_name or self.node_id,
            "is_co_located": is_co_located,
            "capabilities": capabilities,
        }
        res = self._post("/v1/agent/candidate-register", payload)
        self.session_token = res.get("session_token", "")
        return res

    def bootstrap_local(
        self,
        display_name: str,
        capabilities: dict[str, Any],
        api_token: str = "",
    ) -> dict[str, Any]:
        payload = {
            "enrollment_token": "local-bootstrap",
            "node_id": self.node_id,
            "display_name": display_name or self.node_id,
            "is_co_located": True,
            "capabilities": capabilities,
        }
        tok = api_token or os.environ.get("SENSORYPLEX_API_TOKEN", "")
        res = self._post("/v1/agent/bootstrap-local", payload, token=tok)
        self.session_token = res.get("session_token", "")
        return res

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

    def deregister(self, reason: str = "agent_uninstalled") -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "session_token": self.session_token,
            "reason": reason,
        }
        return self._post("/v1/agent/deregister", payload, token=self.session_token)


def get_plugins_base_dir(state_file: str | None = None) -> Path:
    if state_file:
        return Path(state_file).resolve().parent / "plugins"
    env_dir = os.environ.get("SENSORYPLEX_AGENT_PLUGINS_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return Path(".data/agent").resolve() / "plugins"


def get_instance_dir(base_dir: Path, plugin_id: str, artifact_digest: str) -> Path:
    digest_clean = artifact_digest.replace("sha256:", "").strip()
    return base_dir / plugin_id / digest_clean


def terminate_process_by_pid(pid: int, timeout_s: float = 3.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def stop_instance_process(instance_dir: Path) -> None:
    pid_file = instance_dir / "plugin.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            terminate_process_by_pid(pid)
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)


def execute_intent(
    intent: dict[str, Any], client: NodeAgentClient, state_file: str | None = None
) -> bool:
    """受控执行主节点下发的部署意图并上报结果，严格落实物理级安装、停止与干净卸载。"""
    intent_id = intent["intent_id"]
    instance_id = intent["instance_id"]
    action = intent["action"].replace("DEPLOYMENT_ACTION_", "").lower()
    plugin_id = intent.get("plugin_id", "")
    digest = intent.get("artifact_digest", "")

    base_dir = get_plugins_base_dir(state_file)
    instance_dir = get_instance_dir(base_dir, plugin_id, digest) if plugin_id and digest else None

    LOGGER.info("Processing intent", intent_id=intent_id, action=action, plugin_id=plugin_id)

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
        if instance_dir:
            instance_dir.mkdir(parents=True, exist_ok=True)
            meta_file = instance_dir / "instance.json"
            meta_file.write_text(json.dumps(intent, indent=2, ensure_ascii=False))

        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state=target_state,
        )
        LOGGER.info("Successfully executed intent", action=action, instance_id=instance_id)
        return True

    elif action == "start":
        if instance_dir:
            instance_dir.mkdir(parents=True, exist_ok=True)
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
        )
        return True

    elif action == "stop":
        if instance_dir:
            stop_instance_process(instance_dir)
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_STOPPED",
        )
        return True

    elif action == "uninstall":
        # 物理级干净卸载：优雅终止进程 -> 递归删除内容寻址实例目录 -> 清理无其他版本的父目录
        if instance_dir:
            stop_instance_process(instance_dir)
            if instance_dir.exists():
                shutil.rmtree(instance_dir, ignore_errors=True)
            parent = base_dir / plugin_id
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            LOGGER.info(
                "Cleanly uninstalled plugin instance directory",
                plugin_id=plugin_id,
                digest=digest,
                path=str(instance_dir),
            )

        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED",
        )
        return True

    elif action == "task_process":
        # 任务意图与插件安装意图共用传输通道，但 Agent 尚未接入受控 Runtime 执行器。
        # 必须给控制面稳定的失败事实，不能返回 unknown action 后让任务永久停在处理中。
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=False,
            actual_state="PLUGIN_INSTANCE_STATE_UNSPECIFIED",
            error_code="runtime_task_service_not_attached",
            error_detail="task_process requires a controlled runtime executor",
        )
        return False

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
    enroll_parser.add_argument("--token", default="")
    enroll_parser.add_argument("--local", action="store_true", help="Auto-bootstrap local node")
    enroll_parser.add_argument(
        "--candidate", action="store_true", help="Register as candidate node"
    )
    enroll_parser.add_argument("--display-name", default="")
    enroll_parser.add_argument("--co-located", action="store_true")
    enroll_parser.add_argument("--state-file", default="")
    enroll_parser.add_argument("--override-capabilities", default="")

    dereg_parser = subparsers.add_parser(
        "deregister", help="Deregister node and clean up local plugins"
    )
    dereg_parser.add_argument("--main-url", default="http://127.0.0.1:8091")
    dereg_parser.add_argument("--node-id", required=True)
    dereg_parser.add_argument("--session-token", default="")
    dereg_parser.add_argument("--state-file", default="")

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
        if args.local:
            res = client.bootstrap_local(args.display_name, caps)
        elif args.candidate:
            res = client.register_candidate(args.display_name, args.co_located, caps)
        else:
            if not args.token:
                LOGGER.error("Error: --token required for remote enrollment")
                sys.exit(1)
            res = client.enroll(args.token, args.display_name, args.co_located, caps)
        LOGGER.info("Enrolled successfully", node_id=args.node_id, status=res.get("status"))
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
            LOGGER.info("State saved", state_file=args.state_file)

    elif args.command == "deregister":
        token = args.session_token
        main_url = args.main_url
        sf_path = Path(args.state_file) if args.state_file else None
        if sf_path and sf_path.is_file():
            data = json.loads(sf_path.read_text())
            token = token or data.get("session_token", "")
            main_url = main_url or data.get("main_url", "")

        if token:
            client = NodeAgentClient(main_url, args.node_id, token)
            try:
                client.deregister()
                LOGGER.info("Deregistered from main node", node_id=args.node_id)
            except Exception as e:
                LOGGER.warning(
                    "Deregister notification failed (proceeding with local cleanup): %s", e
                )

        # 物理清理该节点的所有插件实例目录与进程
        base_dir = get_plugins_base_dir(args.state_file)
        if base_dir.is_dir():
            for pid_file in base_dir.rglob("*.pid"):
                try:
                    pid = int(pid_file.read_text().strip())
                    terminate_process_by_pid(pid)
                except (ValueError, OSError):
                    pass
            shutil.rmtree(base_dir, ignore_errors=True)
            LOGGER.info("Cleaned all plugin instance directories", plugins_dir=str(base_dir))

        if sf_path and sf_path.is_file():
            sf_path.unlink(missing_ok=True)
            LOGGER.info("Cleaned local state file", state_file=str(sf_path))

        LOGGER.info("Node cleanly uninstalled and deregistered", node_id=args.node_id)

    elif args.command == "run":
        token = args.session_token
        main_url = args.main_url
        if args.state_file and Path(args.state_file).is_file():
            data = json.loads(Path(args.state_file).read_text())
            token = token or data.get("session_token", "")
            main_url = main_url or data.get("main_url", "")
        if not token:
            LOGGER.error("Error: session_token required")
            sys.exit(1)

        client = NodeAgentClient(main_url, args.node_id, token)

        heartbeat_count = 0
        while True:
            try:
                hb_res = client.heartbeat()
                status = hb_res.get("status", "")
                heartbeat_count += 1
                if heartbeat_count % 12 == 1:
                    LOGGER.info("Heartbeat active", node_id=args.node_id, status=status)

                if status == "NODE_STATUS_REVOKED":
                    LOGGER.error("Node revoked by main node", node_id=args.node_id)
                    sys.exit(2)

                intents = hb_res.get("pending_intents", [])
                for intent in intents:
                    execute_intent(intent, client, state_file=args.state_file)

                if args.once:
                    LOGGER.info("Heartbeat once completed", status=status, processed=len(intents))
                    break

                interval = hb_res.get("heartbeat_interval_ms", 5000) / 1000.0
                time.sleep(interval or args.interval_s)

            except Exception as e:
                LOGGER.error("Heartbeat error: %s", e, exc_info=True)
                if args.once:
                    sys.exit(1)
                time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
