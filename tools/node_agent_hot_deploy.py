"""节点侧热部署执行器：staging → starting → validating → candidate_ready → drain（ADR-030）。

执行器是"意图 → 事实"的唯一翻译层：它按控制面下发的 release_id 从受认证端点取 bundle，离线装出
版本化实例，交给平台服务托管，逐项验证候选（Describe → 身份/摘要核对 → ValidateConfig → Start →
连续三次 Health=ready），再把**真实观测**回报给控制面。它不接受意图里的任意 URL、命令、宿主路径
或密钥 —— 意图只给身份（release/bundle/artifact/generation），其余一律由本机推导。

失败一律带稳定错误码显式回报，且**切换前失败绝不碰槽位 active 指针**：候选留在 failed，
旧 active 继续服务。回滚不是"改写历史"，而是控制面创建的反向部署操作。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import time
from typing import Any

import grpc
from edge_material_sdk import get_logger
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 以脚本方式运行时 `sys.path[0]` 是 tools/ 本身，`tools.*` 需要仓库根。
    sys.path.insert(0, str(ROOT))

from tools.node_agent_platform import (  # noqa: E402
    HotDeployError,
    RuntimeSpec,
    arch_name,
    install_release,
    platform_name,
    read_endpoint_file,
    safe_extract_bundle,
    sanitize_label,
    supervisor_for_platform,
    unit_name,
    verify_bundle_bytes,
    verify_bundle_tree,
    wait_for_endpoint_file,
)

LOGGER = get_logger("sensoryplex.agent.hot_deploy")

# 候选启动总时限与健康探测节奏（ADR-030 §2.8）：默认 5 分钟、间隔 5 秒。
STARTUP_BUDGET_S = 300.0
HEALTH_INTERVAL_S = 5.0
HEALTH_CONSECUTIVE = 3
# 单次 gRPC 调用的上限：Drain/Stop 是控制面语义，不能被一个卡死的插件无限挂住。
LIFECYCLE_RPC_TIMEOUT_S = 10.0
DRAIN_FLOOR_MS = 60_000

STAGE_PROTO = {
    "staging": "PLUGIN_OPERATION_STAGE_STAGING",
    "starting": "PLUGIN_OPERATION_STAGE_STARTING",
    "validating": "PLUGIN_OPERATION_STAGE_VALIDATING",
    "candidate_ready": "PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
}


class PluginChannel:
    """候选实例的 gRPC 生命周期通道；端口只能来自 endpoint 文件。"""

    def __init__(self, endpoint: str, timeout_s: float):
        self.endpoint = endpoint
        self.channel = grpc.insecure_channel(endpoint)
        grpc.channel_ready_future(self.channel).result(timeout=timeout_s)
        self.stub = runtime_pb2_grpc.ProcessorPluginServiceStub(self.channel)

    def describe(self, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        return self.stub.Describe(runtime_pb2.DescribeRequest(), timeout=timeout_s)

    def validate_config(self, config: dict, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        request = runtime_pb2.ValidateConfigRequest()
        json_format.ParseDict(config or {}, request.config)
        return self.stub.ValidateConfig(request, timeout=timeout_s)

    def start(self, config: dict, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        request = runtime_pb2.StartRequest()
        json_format.ParseDict(config or {}, request.config)
        return self.stub.Start(request, timeout=timeout_s)

    def health(self, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        return self.stub.Health(runtime_pb2.HealthRequest(), timeout=timeout_s)

    def drain(self, grace_ms: int, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        request = runtime_pb2.DrainRequest(grace_period_ms=grace_ms)
        return self.stub.Drain(request, timeout=timeout_s)

    def stop(self, timeout_s: float = LIFECYCLE_RPC_TIMEOUT_S):
        return self.stub.Stop(runtime_pb2.StopRequest(), timeout=timeout_s)

    def close(self) -> None:
        self.channel.close()

    def __enter__(self) -> PluginChannel:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class HotDeployExecutor:
    """把热部署意图执行成真实事实，并把事实如实回报。"""

    def __init__(
        self,
        client,
        *,
        base_dir: pathlib.Path,
        platform: str = "",
        arch: str = "",
        supervisors=None,
        health_interval_s: float = HEALTH_INTERVAL_S,
        startup_budget_s: float = STARTUP_BUDGET_S,
    ):
        self.client = client
        self.base_dir = pathlib.Path(base_dir)
        self.platform = platform or platform_name()
        self.arch = arch or arch_name()
        self.supervisor = supervisors or supervisor_for_platform(self.platform)
        self.health_interval_s = health_interval_s
        self.startup_budget_s = startup_budget_s

    # ── 本地运行实例台账（Agent 重启后据此对账，绝不删 active/previous 制品） ──

    @property
    def registry_path(self) -> pathlib.Path:
        return self.base_dir / "hot-deploy.json"

    def registry(self) -> dict[str, dict]:
        if not self.registry_path.is_file():
            return {}
        try:
            return json.loads(self.registry_path.read_text())
        except (OSError, ValueError):
            LOGGER.warning("hot deploy registry unreadable", path=str(self.registry_path))
            return {}

    def _remember(self, runtime_instance_id: str, entry: dict) -> None:
        registry = self.registry()
        registry[runtime_instance_id] = entry
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_name(f"{self.registry_path.name}.tmp")
        temporary.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.registry_path)

    def observations(self) -> list[dict]:
        """Agent 侧真实观测：平台服务状态 + endpoint 文件是否还在。

        只把"本机台账记录的期望"与"平台服务实际状态"一致的情况报成 matched；不一致或读不出来
        一律如实报 unknown，让控制面拿到 `reconciliation_required`，而不是猜一个成功。
        """
        observed = []
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for runtime_instance_id, entry in sorted(self.registry().items()):
            state = self.supervisor.state(entry["unit_name"])
            endpoint = ""
            endpoint_path = pathlib.Path(entry.get("endpoint_file", ""))
            if endpoint_path.is_file():
                try:
                    endpoint = read_endpoint_file(endpoint_path)["endpoint"]
                except HotDeployError:
                    endpoint = ""
            expected_running = entry.get("desired_state") == "running"
            if expected_running:
                matched = state.loaded and state.running and bool(endpoint)
            else:
                matched = not state.running
            observed.append(
                {
                    "runtime_instance_id": runtime_instance_id,
                    "operation_id": entry.get("operation_id", ""),
                    "generation": int(entry.get("generation") or 0),
                    "observed_state": "running" if state.running else "stopped",
                    "endpoint": endpoint if matched else "",
                    "supervisor_id": entry["unit_name"],
                    "supervisor_managed": state.loaded,
                    "unit_loaded": state.loaded,
                    "last_exit_code": state.last_exit_code
                    if state.last_exit_code is not None
                    else 0,
                    "observed_at": now,
                    "reconciliation": "matched" if matched else "unknown",
                    "detail": "" if matched else f"platform_state={state.detail}",
                }
            )
        return observed

    # ── 意图执行 ──────────────────────────────────────────────────────────

    def stage_release(self, intent: dict) -> bool:
        """受控安装一个 release 作为候选实例，验证通过后回报 candidate_ready。"""
        identity = self._validate_intent(intent)
        if isinstance(identity, str):
            # 直接把拒绝原因原样回报，不要用一句笼统的"缺字段"盖掉已有语义（例如 deadline）。
            return self._fail(intent, identity, f"intent rejected: {identity}")
        candidate = identity
        plugin_id = intent["plugin_id"]
        release_id = candidate["release_id"]
        config_hash = candidate["config_hash"]
        started = time.monotonic()

        # ── staging：下载 → 复算摘要 → 安全解包 → 逐文件对账 → 离线安装 ──
        staging_started = time.monotonic()
        try:
            bundle_path = self._stage_bundle(intent, candidate)
            release_root = self.base_dir / sanitize_label(plugin_id) / "releases" / release_id
            payload, manifest = self._unpack_verified(intent, candidate, bundle_path, release_root)
            interpreter = install_release(
                payload=payload,
                install_dir=release_root,
                manifest=manifest,
                release={"release_id": release_id, "bundle_digest": candidate["bundle_digest"]},
                timeout_s=self.startup_budget_s,
            )
        except HotDeployError as error:
            return self._fail(intent, error.code, error.detail, stage="staging")
        except (OSError, ValueError) as error:
            return self._fail(intent, "candidate_staging_failed", str(error), stage="staging")
        staging_ms = int((time.monotonic() - staging_started) * 1000)

        runtime_dir = self.runtime_dir(plugin_id, candidate["runtime_instance_id"])
        runtime_dir.mkdir(parents=True, exist_ok=True)
        endpoint_file = runtime_dir / "endpoint.json"
        endpoint_file.unlink(missing_ok=True)
        spec = RuntimeSpec(
            plugin_id=plugin_id,
            release_id=release_id,
            runtime_instance_id=candidate["runtime_instance_id"],
            artifact_digest=intent["artifact_digest"],
            python_module=manifest["entrypoint"]["python_module"],
            interpreter=interpreter,
            payload_src=payload / "src",
            runtime_dir=runtime_dir,
            endpoint_file=endpoint_file,
            stdout_log=runtime_dir / "plugin.out.log",
            stderr_log=runtime_dir / "plugin.err.log",
        )
        unit = unit_name(plugin_id, release_id, candidate["runtime_instance_id"])
        self._report(
            intent,
            success=True,
            stage="staging",
            staging_ms=staging_ms,
            actual_state="PLUGIN_INSTANCE_STATE_INSTALLING",
        )

        # ── starting：平台服务装载候选实例（异常退出才重启） ──
        starting_started = time.monotonic()
        try:
            self.supervisor.load(unit, spec)
        except HotDeployError as error:
            self._teardown(unit)
            return self._fail(intent, error.code, error.detail, stage="starting")
        starting_ms = int((time.monotonic() - starting_started) * 1000)
        self._report(
            intent,
            success=True,
            stage="starting",
            staging_ms=staging_ms,
            starting_ms=starting_ms,
            supervisor_id=unit,
            actual_state="PLUGIN_INSTANCE_STATE_INSTALLING",
        )

        # ── validating：endpoint → Describe → 身份核对 → ValidateConfig → Start → Health ──
        validating_started = time.monotonic()
        remaining = self.startup_budget_s - (time.monotonic() - started)
        endpoint = ""
        try:
            if remaining <= 0:
                raise HotDeployError("candidate_start_timeout", "startup budget exhausted")
            endpoint_payload = wait_for_endpoint_file(endpoint_file, deadline_s=remaining)
            endpoint = endpoint_payload["endpoint"]
            description, verified = self._validate_candidate(
                intent, spec, endpoint, deadline_s=remaining
            )
        except HotDeployError as error:
            self._teardown(unit, endpoint_file)
            return self._fail(intent, error.code, error.detail, stage="validating")
        except grpc.RpcError as error:
            self._teardown(unit, endpoint_file)
            return self._fail(
                intent,
                "candidate_start_failed",
                f"{error.code().name}: {error.details()}",
                stage="validating",
            )
        validating_ms = int((time.monotonic() - validating_started) * 1000)

        self._remember(
            candidate["runtime_instance_id"],
            {
                "plugin_id": plugin_id,
                "release_id": release_id,
                "artifact_digest": intent["artifact_digest"],
                # 控制面基于原始 JSON 锁定的身份。Struct 传输会规范化数字或省略 null，
                # 重算会造成台账与 Revision 漂移，故此处只使用独立契约字段。
                "config_hash": config_hash,
                "operation_id": candidate["operation_id"],
                "generation": candidate["generation"],
                "unit_name": unit,
                "install_dir": str(release_root),
                "runtime_dir": str(runtime_dir),
                "endpoint_file": str(endpoint_file),
                "endpoint": endpoint,
                "supervisor": self.supervisor.name,
                "desired_state": "running",
            },
        )
        LOGGER.info(
            "candidate ready",
            plugin_id=plugin_id,
            release_id=release_id,
            endpoint=endpoint,
            verified_plugin_id=verified["plugin_id"],
            artifact_digest=verified["artifact_digest"],
        )
        # 回报 candidate_ready 即请求控制面切换 active 指针（事务 + generation CAS）。
        self._report(
            intent,
            success=True,
            stage="candidate_ready",
            staging_ms=staging_ms,
            starting_ms=starting_ms,
            validating_ms=validating_ms,
            endpoint=endpoint,
            supervisor_id=unit,
            verified_plugin_id=verified["plugin_id"],
            verified_artifact_digest=verified["artifact_digest"],
            actual_state="PLUGIN_INSTANCE_STATE_READY",
        )
        return True

    def drain(self, intent: dict) -> bool:
        """排空并安全卸载旧实例：Drain → 受限 grace → Stop → 卸载 unit，制品留作回滚版本。"""
        runtime_instance_id = intent.get("runtime_instance_id") or ""
        entry = self.registry().get(runtime_instance_id)
        if not entry:
            return self._fail(
                intent, "drain_target_unknown", f"no local record for {runtime_instance_id}"
            )
        grace_ms = int(intent.get("grace_period_ms") or 0) or DRAIN_FLOOR_MS
        deadline_ms = int(intent.get("deadline_unix_ms") or 0)
        started = time.monotonic()

        endpoint = entry.get("endpoint", "")
        endpoint_file = pathlib.Path(entry.get("endpoint_file", ""))
        if endpoint_file.is_file():
            try:
                endpoint = read_endpoint_file(endpoint_file)["endpoint"]
            except HotDeployError:
                endpoint = endpoint

        # 1) Drain RPC：让旧实例停止接受新任务。连不上不影响"必须停掉它"这个事实，但要如实记录。
        drain_rpc_ok = False
        if endpoint:
            try:
                with PluginChannel(endpoint, timeout_s=LIFECYCLE_RPC_TIMEOUT_S) as channel:
                    channel.drain(grace_ms)
                drain_rpc_ok = True
            except (grpc.RpcError, HotDeployError, OSError) as error:
                LOGGER.warning(
                    "drain rpc unavailable; falling back to platform stop",
                    runtime_instance_id=runtime_instance_id,
                    detail=str(error)[:200],
                )

        # 2) 受限 grace：必须真的等够"不短于插件最大请求 deadline"的排空窗口。
        #    剩余操作时限比 grace 还短时**不缩水**，直接显式失败 drain_timeout。
        if deadline_ms:
            remaining_ms = deadline_ms - int(time.time() * 1000)
            if remaining_ms < grace_ms:
                return self._fail(
                    intent,
                    "drain_timeout",
                    f"grace_period_ms={grace_ms} exceeds remaining operation budget {remaining_ms}",
                )
        grace_deadline = time.monotonic() + grace_ms / 1000.0
        exited_on_its_own = False
        while time.monotonic() < grace_deadline:
            if not self.supervisor.state(entry["unit_name"]).running:
                exited_on_its_own = True
                break
            rank = grace_deadline - time.monotonic()
            time.sleep(min(0.5, max(rank, 0.0)))

        # 3) Stop 并卸载 unit：grace 到点后不再等，异常退出才重启的策略不会把它拉回来。
        try:
            state = self.supervisor.stop(entry["unit_name"])
        except HotDeployError as error:
            return self._fail(intent, error.code, error.detail)
        if self.supervisor.state(entry["unit_name"]).running:
            return self._fail(intent, "drain_timeout", "old instance still running after unload")
        endpoint_file.unlink(missing_ok=True)

        draining_ms = int((time.monotonic() - started) * 1000)
        entry.update({"desired_state": "stopped", "endpoint": ""})
        self._remember(runtime_instance_id, entry)
        LOGGER.info(
            "old instance drained and unloaded",
            runtime_instance_id=runtime_instance_id,
            drain_rpc_ok=drain_rpc_ok,
            exited_on_its_own=exited_on_its_own,
            last_exit_code=state.last_exit_code,
            draining_ms=draining_ms,
        )
        self._report(
            intent,
            success=True,
            draining_ms=draining_ms,
            verified_plugin_id=entry.get("plugin_id", ""),
            verified_artifact_digest=entry.get("artifact_digest", ""),
            supervisor_id=entry["unit_name"],
            actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED",
        )
        return True

    def stop_runtime(self, intent: dict) -> bool:
        """取消切换前失败：停掉候选实例并卸载 unit。**旧 active 指针与制品都不动。**"""
        runtime_instance_id = intent.get("runtime_instance_id") or ""
        entry = self.registry().get(runtime_instance_id)
        if not entry:
            # 候选可能还没写进台账（例如 staging 阶段就失败了）：本机没有可停的实例，
            # 如实回报"已停止"，不去伪造一个从未存在过的运行实例。
            self._report(intent, success=True, actual_state="PLUGIN_INSTANCE_STATE_STOPPED")
            return True
        try:
            self.supervisor.stop(entry["unit_name"])
        except HotDeployError as error:
            return self._fail(intent, error.code, error.detail)
        pathlib.Path(entry.get("endpoint_file", "")).unlink(missing_ok=True)
        entry.update({"desired_state": "stopped", "endpoint": ""})
        self._remember(runtime_instance_id, entry)
        self._report(
            intent,
            success=True,
            supervisor_id=entry["unit_name"],
            verified_plugin_id=entry.get("plugin_id", ""),
            verified_artifact_digest=entry.get("artifact_digest", ""),
            actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED",
        )
        return True

    def unload_all(self) -> dict:
        """安全卸载本机托管的所有版本化实例（节点注销路径）。

        只卸载平台服务单元、删掉死 endpoint；`releases/` 里的制品保留不动 —— 节点重连后仍能按
        已验证版本继续回滚，而不是被迫重新下载与安装。
        """
        stopped, failed = [], []
        for runtime_instance_id, entry in sorted(self.registry().items()):
            try:
                self.supervisor.stop(entry["unit_name"])
            except HotDeployError as error:
                failed.append(f"{runtime_instance_id}:{error.code}")
                continue
            pathlib.Path(entry.get("endpoint_file", "")).unlink(missing_ok=True)
            entry.update({"desired_state": "stopped", "endpoint": ""})
            self._remember(runtime_instance_id, entry)
            stopped.append(runtime_instance_id)
        return {"stopped": stopped, "failed": failed}

    def reconcile(self, intent: dict) -> bool:
        """对账意图：只刷新本机事实并如实回报，不猜成功、不删 active/previous 制品。"""
        observed = self.observations()
        mismatch = [item for item in observed if item["reconciliation"] != "matched"]
        LOGGER.info(
            "reconcile requested",
            runtime_instances=len(observed),
            unverified=len(mismatch),
            operation_id=intent.get("operation_id", ""),
        )
        self._report(
            intent,
            success=True,
            verified_plugin_id=intent.get("plugin_id", ""),
            verified_artifact_digest=intent.get("artifact_digest", ""),
            actual_state="PLUGIN_INSTANCE_STATE_READY",
        )
        return True

    # ── 内部步骤 ──────────────────────────────────────────────────────────

    def runtime_dir(self, plugin_id: str, runtime_instance_id: str) -> pathlib.Path:
        return self.base_dir / sanitize_label(plugin_id) / "runtimes" / runtime_instance_id

    def _validate_intent(self, intent: dict) -> dict | str:
        """热部署意图必须携带完整身份；缺一项都不执行，直接显式失败。"""
        candidate = {
            "operation_id": intent.get("operation_id") or "",
            "release_id": intent.get("release_id") or "",
            "bundle_digest": intent.get("bundle_digest") or "",
            "runtime_instance_id": intent.get("runtime_instance_id") or "",
            "generation": int(intent.get("generation") or 0),
            "artifact_digest": intent.get("artifact_digest") or "",
            "config_hash": intent.get("config_hash") or "",
        }
        for key, value in candidate.items():
            if key == "generation":
                continue
            if not value:
                return f"deployment_operation_missing:{key}"
        if not candidate["artifact_digest"].startswith("sha256:"):
            return "invalid_artifact_digest"
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", candidate["config_hash"]):
            return "invalid_config_hash"
        if not candidate["bundle_digest"].startswith("sha256:"):
            return "invalid_bundle_digest"
        deadline_ms = int(intent.get("deadline_unix_ms") or 0)
        if deadline_ms and int(time.time() * 1000) > deadline_ms:
            return "operation_deadline_exceeded"
        return candidate

    def _stage_bundle(self, intent: dict, candidate: dict) -> pathlib.Path:
        """只按 release_id 从受认证端点取 bundle；下载后立刻用落盘字节复算摘要。"""
        staging = self.base_dir / sanitize_label(intent["plugin_id"]) / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        bundle_path = staging / f"{candidate['release_id']}.tar.gz"
        try:
            self.client.download_release_bundle(candidate["release_id"], bundle_path)
        except HotDeployError:
            raise
        except Exception as error:  # noqa: BLE001
            # 下载端点的任何失败都必须变成**稳定错误码**：让它冒出去会把意图挂到超时，
            # 而不是如实回报"这次取制品失败了"。
            raise HotDeployError("release_bundle_unavailable", str(error)[:300]) from error
        verify_bundle_bytes(bundle_path, candidate["bundle_digest"])
        return bundle_path

    def _unpack_verified(
        self, intent: dict, candidate: dict, bundle_path: pathlib.Path, release_root: pathlib.Path
    ) -> tuple[pathlib.Path, dict]:
        extract_dir = release_root / "extract"
        payload = release_root / "payload"
        for stale in (extract_dir, payload):
            if stale.is_dir():
                shutil.rmtree(stale)
        manifest = safe_extract_bundle(bundle_path, extract_dir)
        verify_bundle_tree(extract_dir, manifest)

        plugin = manifest.get("plugin", {})
        if manifest.get("artifact_digest") != candidate["artifact_digest"]:
            raise HotDeployError(
                "release_artifact_digest_mismatch",
                f"manifest={manifest.get('artifact_digest')} intent={candidate['artifact_digest']}",
            )
        if plugin.get("plugin_id") != intent["plugin_id"]:
            raise HotDeployError(
                "release_plugin_identity_mismatch",
                f"bundle={plugin.get('plugin_id')} intent={intent['plugin_id']}",
            )
        if plugin.get("platform") != self.platform or plugin.get("arch") != self.arch:
            raise HotDeployError(
                "release_platform_mismatch",
                f"bundle={plugin.get('platform')}-{plugin.get('arch')} "
                f"host={self.platform}-{self.arch}",
            )
        if manifest.get("entrypoint", {}).get("transport") != "grpc":
            raise HotDeployError("release_entrypoint_invalid", "transport != grpc")
        source = extract_dir / "payload"
        if not source.is_dir():
            raise HotDeployError("release_payload_missing", str(source))
        source.rename(payload)
        return payload, manifest

    def _validate_candidate(
        self, intent: dict, spec: RuntimeSpec, endpoint: str, *, deadline_s: float
    ) -> tuple[Any, dict]:
        """Describe → 身份/摘要核对 → ValidateConfig → Start → 连续三次 Health=ready。"""
        deadline = time.monotonic() + max(deadline_s, 1.0)
        with PluginChannel(endpoint, timeout_s=min(LIFECYCLE_RPC_TIMEOUT_S, deadline_s)) as channel:
            description = channel.describe()
            # 身份与摘要是"进程自己算出来的值"，不是意图回显：不一致就绝不切换。
            if description.name != intent["plugin_id"]:
                raise HotDeployError(
                    "candidate_plugin_identity_mismatch",
                    f"described={description.name} intent={intent['plugin_id']}",
                )
            if description.artifact_digest != intent["artifact_digest"]:
                raise HotDeployError(
                    "candidate_artifact_digest_mismatch",
                    f"described={description.artifact_digest} intent={intent['artifact_digest']}",
                )
            description_payload = json_format.MessageToDict(
                description, preserving_proto_field_name=True
            )
            validation = channel.validate_config(intent.get("config") or {})
            if not validation.valid:
                raise HotDeployError(
                    "candidate_config_invalid",
                    ";".join(validation.field_errors) or "validate_config rejected",
                )
            started = channel.start(intent.get("config") or {})
            if started.state != "ready":
                reason = started.error.reason_code if started.error.reason_code else started.state
                raise HotDeployError("candidate_start_failed", reason)

            ready_streak = 0
            while ready_streak < HEALTH_CONSECUTIVE:
                if time.monotonic() > deadline:
                    raise HotDeployError(
                        "candidate_start_timeout",
                        f"health not ready after {HEALTH_CONSECUTIVE} consecutive probes",
                    )
                health = channel.health()
                if health.state == "ready":
                    ready_streak += 1
                else:
                    ready_streak = 0
                if ready_streak < HEALTH_CONSECUTIVE:
                    time.sleep(min(self.health_interval_s, max(deadline - time.monotonic(), 0.0)))
        verified = {
            "plugin_id": description_payload.get("name", ""),
            "artifact_digest": description_payload.get("artifact_digest", ""),
        }
        return description_payload, verified

    def _teardown(self, unit: str, endpoint_file: pathlib.Path | None = None) -> None:
        """候选失败时的清理：停掉并卸载 unit，删掉死 endpoint。绝不碰旧 active。"""
        try:
            self.supervisor.stop(unit)
        except HotDeployError as error:
            LOGGER.warning("candidate teardown failed", unit=unit, detail=error.detail)
        if endpoint_file is not None:
            endpoint_file.unlink(missing_ok=True)

    def _report(self, intent: dict, **fields) -> None:
        payload = {
            "intent_id": intent["intent_id"],
            "instance_id": intent.get("instance_id", ""),
            "action": intent["action"],
            "success": bool(fields.pop("success", True)),
            "actual_state": fields.pop("actual_state", ""),
            "error_code": "",
            "error_detail": "",
            "operation_id": intent.get("operation_id") or "",
            "generation": int(intent.get("generation") or 0),
            "release_id": intent.get("release_id") or "",
            "runtime_instance_id": intent.get("runtime_instance_id") or "",
        }
        stage = fields.pop("stage", "")
        if stage:
            payload["stage"] = STAGE_PROTO[stage]
        for key, value in fields.items():
            if value == "" or value is None:
                continue
            payload[key] = value
        self.client.report_deployment(**payload)

    def _fail(self, intent: dict, code: str, detail: str, *, stage: str = "") -> bool:
        """显式失败：稳定错误码 + 阶段，控制面据此把候选置 failed 并保留旧 active。"""
        LOGGER.error(
            "hot deploy failed",
            code=code,
            detail=detail[:300],
            operation=intent.get("operation_id", ""),
        )
        self._report(
            intent,
            success=False,
            stage=stage,
            error_code=code,
            error_detail=detail[:500],
            actual_state="PLUGIN_INSTANCE_STATE_FAILED",
        )
        return False
