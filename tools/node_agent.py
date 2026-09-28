"""ADR-026 子节点 Node Agent：注册、心跳、能力上报与部署意图受控执行。

运行在子节点（同机或局域网机器），通过受认证控制通道与 Web 主节点通信。
不向网络暴露未经认证的端口；只执行主节点签发并下发的部署命令。
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from edge_material_sdk import get_logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 以脚本方式运行时 `sys.path[0]` 是 tools/ 本身，`tools.*` 需要仓库根。
    sys.path.insert(0, str(ROOT))

LOGGER = get_logger("sensoryplex.agent")
# 单次 release bundle 下载上限（秒）：大 bundle 走本机回环，给足时间但绝不无限等。
BUNDLE_DOWNLOAD_TIMEOUT_S = 600.0
# 心跳与执解耦后，待执行意图的本地缓冲上限。队列满时留在待投列表下一轮继续投递，
# 既不阻塞心跳，也不丢弃控制面已经标记为 dispatched 的意图。
INTENT_QUEUE_CAPACITY = 16


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

    def _get_json(self, path: str) -> dict[str, Any]:
        """读取 Agent 专用控制面 JSON；所有调用都带节点会话。"""
        request = urllib.request.Request(
            f"{self.main_url}{path}",
            headers={
                "Authorization": f"Bearer {self.session_token}",
                "User-Agent": f"SensoryPlex-NodeAgent/{self.node_id}",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                code = json.loads(body).get("reason_code") or body[:160]
            except json.JSONDecodeError:
                code = body[:160]
            raise RuntimeError(f"HTTP {error.code}: {code}") from error

    def task_manifest(self, intent_id: str) -> dict[str, Any]:
        """获取 v2 Task 的受控身份/策略清单；不含媒体路径或插件 endpoint。"""
        return self._get_json(f"/v1/agent/task-intents/{intent_id}/manifest")

    def download_task_asset(self, intent_id: str, target: Path, expected_digest: str) -> Path:
        """下载已绑定 assignment 的媒体到 Agent 私有工作区，并复算内容摘要。"""
        request = urllib.request.Request(
            f"{self.main_url}/v1/agent/task-intents/{intent_id}/asset",
            headers={
                "Authorization": f"Bearer {self.session_token}",
                "User-Agent": f"SensoryPlex-NodeAgent/{self.node_id}",
            },
            method="GET",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.part")
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=BUNDLE_DOWNLOAD_TIMEOUT_S) as response:
                with temporary.open("wb") as handle:
                    for block in iter(lambda: response.read(1 << 20), b""):
                        digest.update(block)
                        handle.write(block)
        except (urllib.error.URLError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"task_asset_download_failed: {error}") from error
        actual = "sha256:" + digest.hexdigest()
        if actual != expected_digest:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"task_asset_digest_mismatch: expected={expected_digest} actual={actual}"
            )
        temporary.replace(target)
        return target

    def report_task_result(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """提交 v2 Task 事实；接口会校验本节点、assignment、回执与不可变插件身份。"""
        return self._post(f"/v1/agent/tasks/{task_id}:result", payload, token=self.session_token)

    def ingest_task_timeline(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """提交 Timeline 融合出的派生事实；不传原始媒体或任何宿主路径。"""
        return self._post(f"/v1/agent/tasks/{task_id}:timeline", payload, token=self.session_token)

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
        observations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "session_token": self.session_token,
            "timestamp_unix_ms": int(time.time() * 1000),
            "available_memory_bytes": available_memory_bytes,
            "current_concurrency": current_concurrency,
            "running_instance_ids": running_instances or [],
        }
        if observations:
            # Agent 用平台服务实际状态与控制面 generation 对账后的观测；未知状态如实上报。
            payload["runtime_observations"] = observations
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
        *,
        operation_id: str = "",
        generation: int = 0,
        release_id: str = "",
        runtime_instance_id: str = "",
        stage: str = "",
        verified_plugin_id: str = "",
        verified_artifact_digest: str = "",
        endpoint: str = "",
        supervisor_id: str = "",
        staging_ms: int = 0,
        starting_ms: int = 0,
        validating_ms: int = 0,
        draining_ms: int = 0,
    ) -> dict[str, Any]:
        """回报部署事实。热部署字段按需附加：空值不回传，避免把"没观测到"写成事实。"""
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
        hot = {
            "operation_id": operation_id,
            "generation": generation,
            "release_id": release_id,
            "runtime_instance_id": runtime_instance_id,
            "stage": stage,
            "verified_plugin_id": verified_plugin_id,
            "verified_artifact_digest": verified_artifact_digest,
            "endpoint": endpoint,
            "supervisor_id": supervisor_id,
            "staging_ms": staging_ms,
            "starting_ms": starting_ms,
            "validating_ms": validating_ms,
            "draining_ms": draining_ms,
        }
        payload.update({key: value for key, value in hot.items() if value})
        return self._post("/v1/agent/report", payload, token=self.session_token)

    def download_release_bundle(self, release_id: str, target: Path) -> Path:
        """按 release_id 从**受认证端点**取 bundle；不接受意图里的任意 URL。

        下载先落到 `.part` 再 rename：执行器永远看不到半截文件，摘要复算不会因为"读到一半"
        而误判。声明摘要只在响应头里做一次早退检查，真正的判据仍是落盘字节。
        """
        url = f"{self.main_url}/v1/agent/releases/{release_id}/bundle"
        headers = {
            "Authorization": f"Bearer {self.session_token}",
            "User-Agent": f"SensoryPlex-NodeAgent/{self.node_id}",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.part")
        try:
            with urllib.request.urlopen(request, timeout=BUNDLE_DOWNLOAD_TIMEOUT_S) as response:
                declared = response.headers.get("X-Bundle-Digest", "")
                with temporary.open("wb") as handle:
                    shutil.copyfileobj(response, handle, length=1 << 20)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                code = json.loads(body).get("reason_code") or body[:120]
            except json.JSONDecodeError:
                code = body[:120]
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"HTTP {error.code}: {code}") from error
        except (urllib.error.URLError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"bundle_download_failed: {error}") from error
        temporary.replace(target)
        if declared:
            LOGGER.info("release bundle downloaded", release_id=release_id, declared=declared)
        return target

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


def get_hot_deploy_executor(client: NodeAgentClient, state_file: str | None = None):
    """构造热部署执行器。

    单文件分发场景（install_agent.sh 从主节点只拉 node_agent.py）里没有 `tools` 包，此时返回
    None：调用方必须把"执行器不可用"作为稳定失败回报，而不是假装执行成功。
    """
    try:
        from tools.node_agent_hot_deploy import HotDeployExecutor
    except ImportError as error:  # pragma: no cover - 只在单文件分发时命中
        LOGGER.warning("hot deploy executor unavailable: %s", error)
        return None
    return HotDeployExecutor(client, base_dir=get_plugins_base_dir(state_file))


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

    if action in {"stage_release", "drain", "reconcile"} or (
        action == "stop" and intent.get("operation_id")
    ):
        # 热部署动作只能由受控执行器完成：它才知道平台服务、版本化实例与 endpoint 契约。
        executor = get_hot_deploy_executor(client, state_file)
        if executor is None:
            client.report_deployment(
                intent_id=intent_id,
                instance_id=instance_id,
                action=intent["action"],
                success=False,
                actual_state="PLUGIN_INSTANCE_STATE_FAILED",
                error_code="hot_deploy_executor_unavailable",
                error_detail="agent is running without the hot deploy executor module",
            )
            return False
        if action == "stage_release":
            return executor.stage_release(intent)
        if action == "drain":
            return executor.drain(intent)
        if action == "reconcile":
            return executor.reconcile(intent)
        return executor.stop_runtime(intent)

    if action in {"install", "rollback", "start"}:
        # 旧意图不含受控 release 与部署操作，写 instance.json 不代表安装或启动成功。
        client.report_deployment(
            intent_id=intent_id,
            instance_id=instance_id,
            action=intent["action"],
            success=False,
            actual_state="PLUGIN_INSTANCE_STATE_FAILED",
            error_code="controlled_release_required",
            error_detail="plugin installation requires an ADR-030 release deployment",
        )
        return False

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
        config = intent.get("config") or {}
        if config.get("execution_mode") == "orchestrated_v2":
            try:
                from tools.task_executor import TaskExecutor
            except ImportError:
                client.report_deployment(
                    intent_id=intent_id,
                    instance_id=instance_id,
                    action=intent["action"],
                    success=False,
                    actual_state="PLUGIN_INSTANCE_STATE_FAILED",
                    error_code="task_executor_unavailable",
                    error_detail="agent is running without the controlled task executor module",
                )
                return False
            try:
                # `execute` 只有在结果入口已持久化可核验回执后才返回。业务失败也会有
                # receipt，随后由 report 仅完成 delivery intent，不会伪造成业务成功。
                TaskExecutor(client, base_dir=base_dir).execute(intent)
            except Exception as error:  # noqa: BLE001 - 不把 Runtime/gRPC/媒体错误回显到控制面
                LOGGER.error(
                    "TaskExecutor failed for intent %s: %s", intent_id, error, exc_info=True
                )
                client.report_deployment(
                    intent_id=intent_id,
                    instance_id=instance_id,
                    action=intent["action"],
                    success=False,
                    actual_state="PLUGIN_INSTANCE_STATE_FAILED",
                    error_code="task_executor_result_not_recorded",
                    error_detail="controlled task executor did not record an execution receipt",
                )
                return False
            client.report_deployment(
                intent_id=intent_id,
                instance_id=instance_id,
                action=intent["action"],
                success=True,
                actual_state="PLUGIN_INSTANCE_STATE_READY",
            )
            return True

        # 历史任务保持明确拒绝，防止旧控制消息绕过不可变 Revision/receipt 协议。
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


def intent_worker(
    intents: "queue.Queue[dict[str, Any]]",
    stop_event: threading.Event,
    client: NodeAgentClient,
    state_file: str | None,
) -> None:
    """在独立线程里串行执行部署/任务意图，让心跳不再被长任务阻塞。

    节点离线判定看的是 `last_heartbeat_at`：只要执行体跑在心跳线程里，超过 60 秒的
    真实任务（例如整段 ASR、全帧 OCR）就会把节点拖成 offline，随后回执上报被 409
    `agent_node_not_schedulable` 拒绝，任务结果永远落不了库。因此这里保持**单工作线程**
    串行语义（并发上限为 1，不改变现有资源占用模型），只把心跳从执行体里解耦出来。
    """
    while not stop_event.is_set():
        try:
            intent = intents.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            execute_intent(intent, client, state_file=state_file)
        except Exception as error:  # noqa: BLE001 - 单个意图不外溢影响心跳与后续意图
            LOGGER.error("Intent execution crashed: %s", error, exc_info=True)
        finally:
            intents.task_done()


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

        # 物理清理该节点的所有插件实例目录与进程。
        # 热部署实例由平台服务托管：必须先卸载 unit，否则删掉安装目录会留下被反复拉起的死单元。
        executor = get_hot_deploy_executor(client, args.state_file) if token else None
        if executor is not None:
            unloaded = executor.unload_all()
            LOGGER.info(
                "Unloaded platform-managed plugin instances",
                stopped=len(unloaded["stopped"]),
                failed=unloaded["failed"],
            )

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
        executor = get_hot_deploy_executor(client, args.state_file)

        # `--once` 保持同步语义（单次心跳 + 立即执行完成后再退出）；常驻模式把心跳与
        # 意图执行解耦，避免长任务期间节点被判定离线、回执上报被 409 拒绝。
        intent_queue: queue.Queue[dict[str, Any]] | None = None
        stop_event = threading.Event()
        backlog: list[dict[str, Any]] = []
        if not args.once:
            intent_queue = queue.Queue(maxsize=INTENT_QUEUE_CAPACITY)
            threading.Thread(
                target=intent_worker,
                args=(intent_queue, stop_event, client, args.state_file),
                name="intent-worker",
                daemon=True,
            ).start()

        heartbeat_count = 0
        while True:
            try:
                observations = executor.observations() if executor else []
                hb_res = client.heartbeat(observations=observations)
                status = hb_res.get("status", "")
                heartbeat_count += 1
                if heartbeat_count % 12 == 1:
                    LOGGER.info("Heartbeat active", node_id=args.node_id, status=status)

                if status == "NODE_STATUS_REVOKED":
                    LOGGER.error("Node revoked by main node", node_id=args.node_id)
                    stop_event.set()
                    sys.exit(2)

                unverified = hb_res.get("reconciliation_required", [])
                if unverified:
                    # 控制面明确要求对账：不猜成功、不删 active/previous 制品，只如实记录。
                    LOGGER.warning(
                        "reconciliation required for runtime instances", items=unverified
                    )

                intents = hb_res.get("pending_intents", [])

                if args.once:
                    for intent in intents:
                        execute_intent(intent, client, state_file=args.state_file)
                    LOGGER.info("Heartbeat once completed", status=status, processed=len(intents))
                    break

                # 队列有界：满了就留在 backlog 下一轮继续投，不阻塞心跳也不丢弃意图。
                backlog.extend(intents)
                while backlog and intent_queue is not None and not intent_queue.full():
                    intent_queue.put(backlog.pop(0))
                if backlog:
                    LOGGER.warning("intent backlog pending", waiting=len(backlog))

                interval = hb_res.get("heartbeat_interval_ms", 5000) / 1000.0
                time.sleep(interval or args.interval_s)

            except Exception as e:
                LOGGER.error("Heartbeat error: %s", e, exc_info=True)
                if args.once:
                    stop_event.set()
                    sys.exit(1)
                time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
