"""Console v2 的本机受控任务执行器。

它只消费 Node Agent 已领取的 `orchestrated_v2` intent：从 API 取得不可变 Task
manifest、从受认证数据面下载本次媒体、从本机 ADR-030 热部署台账读取 active endpoint，
再调用 Runtime 与插件。它不读取数据库、不接受宿主路径/命令/URL，也不调用 legacy
`task_runner.py` 或 `task_worker.py`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
from edge_material_sdk.buffer_reader import LeaseBufferReader, is_loopback_endpoint
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.media.v1 import handoff_pb2, handoff_pb2_grpc, media_pb2
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format

from tools.node_agent_platform import HotDeployError, read_endpoint_file

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUTS_PER_TASK = 32
RUNTIME_WAIT_S = 120.0
RESULT_BODY_LIMIT_HINT = 4_000_000


class TaskExecutionError(RuntimeError):
    """带稳定 reason code 的执行器错误，正文不应包含路径、令牌或媒体内容。"""

    def __init__(
        self,
        reason_code: str,
        *,
        retryable: bool = False,
        inputs: int = 0,
        outputs: int = 0,
        result_ref: str = "",
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
        self.inputs = inputs
        self.outputs = outputs
        self.result_ref = result_ref


@dataclass(frozen=True)
class PluginEndpoint:
    endpoint: str
    plugin_id: str
    artifact_digest: str
    config_hash: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha256_json(value: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _runtime_binary() -> Path:
    configured = os.environ.get("SENSORYPLEX_RUNTIME_BIN", "")
    binary = Path(configured) if configured else ROOT / "target/release/sensoryplex-runtime"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise TaskExecutionError("runtime_binary_unavailable")
    return binary


def _bounded_policy(policy: object) -> dict[str, int]:
    if not isinstance(policy, dict):
        raise TaskExecutionError("task_execution_policy_missing")
    values = {}
    for key, default, lower, upper in (
        ("window_ms", 1_000, 1_000, 1_000),
        ("sample_interval_ms", 1_000, 250, 60_000),
        ("audio_segment_ms", 6_000, 1_000, 60_000),
        ("audio_overlap_ms", 500, 0, 59_000),
        ("vlm_sample_interval_ms", 5_000, 1_000, 60_000),
    ):
        try:
            value = int(policy.get(key, default))
        except (TypeError, ValueError) as error:
            raise TaskExecutionError("task_execution_policy_invalid") from error
        if value < lower or value > upper:
            raise TaskExecutionError("task_execution_policy_invalid")
        values[key] = value
    if values["audio_overlap_ms"] >= values["audio_segment_ms"]:
        raise TaskExecutionError("task_execution_policy_invalid")
    return values


def _runtime_pipeline(policy: dict[str, int], *, has_vlm: bool) -> str:
    """把已签发 Revision 的策略渲染为本次 Runtime 私有输入。

    这不是读取/改写全局 `file-material.yaml`。所有动态数值都来自不可变 Revision manifest，
    生成文件只位于 Agent 私有工作区，Runtime 结束后可与本次 receipt 一起审计。
    """
    enrichment_modalities = "\n      - vision.scene_description" if has_vlm else " []"
    slow_enrichment = (
        """  slow_enrichment:
    - type: vlm
      model: revision-bound
"""
        if has_vlm
        else "  slow_enrichment: []\n"
    )
    return f"""apiVersion: edge.material/v1
kind: Pipeline
metadata:
  name: console-orchestrated-v2
spec:
  source:
    type: file
    uri_secret_ref: AGENT_CONTROLLED_ASSET
  queue_capacity: 32
  data_egress: local_only
  processors:
    - type: adaptive_sampler
    - type: audio_segmenter
    - type: asr
      model: revision-bound
    - type: ocr
      model: revision-bound
    - type: timeline_fusion
{slow_enrichment}  sinks:
    - type: metadata_store
  timeline_fusion:
    window_ms: {policy["window_ms"]}
    fast_modalities:
      - asr_segment
      - ocr_blocks
    enrichment_modalities:{enrichment_modalities}
"""


def _receipt(
    manifest: dict,
    *,
    started_ms: int,
    completed_ms: int,
    inputs: int,
    outputs: int,
    reason: str,
    result_ref: str,
) -> dict:
    task = manifest["task"]
    plugin = manifest["plugin"]
    data = {
        "run_id": task["run_id"] if "run_id" in task else manifest["run"]["run_id"],
        "task_id": task["task_id"],
        "attempt": int(task["attempt"]),
        "assignment_id": task["assignment_id"],
        "plugin_id": plugin["plugin_id"],
        "artifact_digest": plugin["artifact_digest"],
        "config_hash": plugin["config_hash"],
        "input_count": int(inputs),
        "output_count": int(outputs),
        "result_manifest_ref": result_ref,
        "reason_code": reason,
        "started_at": datetime.fromtimestamp(started_ms / 1000, tz=UTC),
        "completed_at": datetime.fromtimestamp(completed_ms / 1000, tz=UTC),
    }
    digest_fields = {
        **{key: value for key, value in data.items() if key not in {"started_at", "completed_at"}},
        "started_at": data["started_at"].isoformat(),
        "completed_at": data["completed_at"].isoformat(),
    }
    data["receipt_digest"] = _sha256_json(digest_fields)
    return {
        **{key: value for key, value in data.items() if key not in {"started_at", "completed_at"}},
        "started_at_unix_ms": started_ms,
        "completed_at_unix_ms": completed_ms,
    }


class TaskExecutor:
    """受控执行一条 v2 Task；业务失败也必须先提交 receipt，再完成 intent。"""

    def __init__(self, client, *, base_dir: Path):
        self.client = client
        self.base_dir = Path(base_dir)

    def execute(self, intent: dict[str, Any]) -> bool:
        intent_id = str(intent.get("intent_id", ""))
        if not intent_id:
            raise TaskExecutionError("task_intent_id_missing")
        manifest = self.client.task_manifest(intent_id)
        self._assert_intent_matches_manifest(intent, manifest)
        task = manifest["task"]
        started_ms = _now_ms()
        inputs, outputs, result_ref = 0, 0, ""
        try:
            if started_ms >= int(task["deadline_unix_ms"]):
                raise TaskExecutionError("task_deadline_exceeded", retryable=True)
            workspace = self._workspace(manifest)
            media_path = self._download_asset(manifest, workspace)
            if task["node_id"] == "timeline_fusion":
                inputs, outputs, result_ref = self._run_timeline(manifest, workspace, media_path)
            else:
                inputs, outputs, result_ref = self._run_plugin(manifest, workspace, media_path)
        except TaskExecutionError as error:
            completed_ms = _now_ms()
            self._report_result(
                manifest,
                intent_id=intent_id,
                success=False,
                retryable=error.retryable,
                reason=error.reason_code,
                inputs=getattr(error, "inputs", inputs),
                outputs=getattr(error, "outputs", outputs),
                started_ms=started_ms,
                completed_ms=completed_ms,
                result_ref=getattr(error, "result_ref", result_ref),
            )
            return True
        except Exception:  # noqa: BLE001 - 外部 Runtime/gRPC 错误不能泄露详情到控制面
            import traceback

            traceback.print_exc()
            completed_ms = _now_ms()
            self._report_result(
                manifest,
                intent_id=intent_id,
                success=False,
                retryable=False,
                reason="task_executor_internal_error",
                inputs=inputs,
                outputs=outputs,
                started_ms=started_ms,
                completed_ms=completed_ms,
                result_ref=result_ref,
            )
            return True
        completed_ms = _now_ms()
        self._report_result(
            manifest,
            intent_id=intent_id,
            success=True,
            retryable=False,
            reason="",
            inputs=inputs,
            outputs=outputs,
            started_ms=started_ms,
            completed_ms=completed_ms,
            result_ref=result_ref,
        )
        return True

    def _assert_intent_matches_manifest(self, intent: dict[str, Any], manifest: dict) -> None:
        config = intent.get("config") or {}
        if config.get("execution_mode") != "orchestrated_v2":
            raise TaskExecutionError("agent_legacy_task_execution_unsupported")
        if (
            manifest.get("intent_id") != intent.get("intent_id")
            or manifest["execution_id"] != config.get("execution_id")
            or manifest["task"]["task_id"] != config.get("task_id")
            or manifest["task"]["assignment_id"] != config.get("assignment_id")
            or int(manifest["task"]["attempt"]) != int(config.get("attempt") or 0)
            or manifest["asset"]["content_hash"] != config.get("content_hash")
        ):
            raise TaskExecutionError("task_manifest_intent_mismatch")

    def _workspace(self, manifest: dict) -> Path:
        execution_id = str(manifest["execution_id"])
        if not execution_id or len(execution_id) > 128 or "/" in execution_id:
            raise TaskExecutionError("task_execution_id_invalid")
        directory = self.base_dir / "task-executions" / execution_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _download_asset(self, manifest: dict, workspace: Path) -> Path:
        asset = manifest["asset"]
        digest = str(asset["content_hash"])
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise TaskExecutionError("task_asset_digest_invalid")
        return self.client.download_task_asset(
            manifest["intent_id"], workspace / f"asset-{digest[7:]}", digest
        )

    def _endpoint(self, manifest: dict) -> PluginEndpoint:
        plugin = manifest["plugin"]
        runtime_instance_id = str(plugin.get("runtime_instance_id", ""))
        if not runtime_instance_id:
            raise TaskExecutionError("plugin_instance_unavailable")
        registry = self.base_dir / "hot-deploy.json"
        try:
            entries = json.loads(registry.read_text(encoding="utf-8"))
            entry = entries[runtime_instance_id]
            endpoint = read_endpoint_file(Path(entry["endpoint_file"]))["endpoint"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, HotDeployError):
            raise TaskExecutionError("plugin_runtime_registry_unavailable") from None
        if (
            entry.get("desired_state") != "running"
            or entry.get("plugin_id") != plugin["plugin_id"]
            or entry.get("artifact_digest") != plugin["artifact_digest"]
            or entry.get("config_hash") != plugin["config_hash"]
            or not is_loopback_endpoint(endpoint)
        ):
            raise TaskExecutionError("plugin_runtime_identity_mismatch")
        channel = grpc.insecure_channel(endpoint)
        try:
            described = runtime_pb2_grpc.ProcessorPluginServiceStub(channel).Describe(
                runtime_pb2.DescribeRequest(), timeout=10
            )
        except grpc.RpcError as error:
            raise TaskExecutionError(
                f"plugin_describe_failed:{error.code().name}", retryable=True
            ) from None
        finally:
            channel.close()
        if (
            described.name != plugin["plugin_id"]
            or described.artifact_digest != plugin["artifact_digest"]
        ):
            raise TaskExecutionError("plugin_runtime_identity_mismatch")
        return PluginEndpoint(
            endpoint=endpoint,
            plugin_id=plugin["plugin_id"],
            artifact_digest=plugin["artifact_digest"],
            config_hash=plugin["config_hash"],
        )

    def _run_plugin(
        self, manifest: dict, workspace: Path, media_path: Path
    ) -> tuple[int, int, str]:
        policy = _bounded_policy(manifest["policy"])
        endpoint = self._endpoint(manifest)
        plugin_id = manifest["plugin"]["plugin_id"]
        kind = (
            "audio_segment"
            if "media.audio_segment" in manifest["plugin"]["consumes"]
            else "video_frame"
        )
        has_vlm = plugin_id == "org.sensoryplex.vlm-moondream"
        report_path, handoff_endpoint, producer = self._run_replay(
            manifest, workspace, media_path, policy, has_vlm
        )
        try:
            channel = grpc.insecure_channel(handoff_endpoint)
            try:
                grpc.channel_ready_future(channel).result(timeout=RUNTIME_WAIT_S)
                handoff = handoff_pb2_grpc.BufferHandoffServiceStub(channel)
                listing = handoff.List(handoff_pb2.ListRetainedRequest(), timeout=20)
            except grpc.RpcError as error:
                raise TaskExecutionError(
                    f"data_plane_list_failed:{error.code().name}", retryable=True
                ) from None
            except grpc.FutureTimeoutError:
                raise TaskExecutionError("data_plane_not_ready", retryable=True) from None
            finally:
                channel.close()
            all_buffers = list(listing.buffers)
            selected = [entry for entry in all_buffers if entry.kind == kind]
            selected.sort(key=lambda entry: (entry.time_range.start_ms, entry.buffer_id))
            if has_vlm:
                selected = self._sample_vlm(selected, policy["vlm_sample_interval_ms"])
            selected = selected[:MAX_INPUTS_PER_TASK]
            source = media_pb2.ReplayReport()
            try:
                source.ParseFromString(report_path.read_bytes())
            except OSError as error:
                raise TaskExecutionError("runtime_replay_report_unavailable") from error
            if kind == "video_frame" and not selected:
                self._discard_buffers(handoff_endpoint, all_buffers)
                raise TaskExecutionError("no_video_frame_buffer_to_process")
            if kind == "audio_segment" and not selected:
                self._discard_buffers(handoff_endpoint, all_buffers)
                if not any(track.track_kind == "audio" for track in source.source.tracks):
                    return self._write_worker_report(
                        workspace,
                        manifest,
                        report_path,
                        [],
                        [],
                        input_count=0,
                        reason="no_audio_track",
                    )
                raise TaskExecutionError("no_audio_segment_buffer_to_process")
            observations, frames, failure = self._process_buffers(
                manifest,
                endpoint,
                source.source.source.source_id,
                selected,
                all_buffers,
                handoff_endpoint,
            )
            _, _, result_ref = self._write_worker_report(
                workspace,
                manifest,
                report_path,
                observations,
                frames,
                input_count=len(selected),
                reason=failure or "",
            )
            if failure:
                raise TaskExecutionError(
                    failure,
                    retryable=failure.startswith("plugin_retryable:"),
                    inputs=len(selected),
                    outputs=len(observations),
                    result_ref=result_ref,
                )
            return len(selected), len(observations), result_ref
        finally:
            self._stop_replay(producer)

    @staticmethod
    def _sample_vlm(buffers: list, interval_ms: int) -> list:
        """VLM 慢路径按 Revision 间隔抽取；未选中的帧会在 coverage 中标明策略原因。"""
        selected, last = [], None
        for entry in buffers:
            if last is None or entry.time_range.start_ms - last >= interval_ms:
                selected.append(entry)
                last = entry.time_range.start_ms
        return selected

    def _run_replay(
        self,
        manifest: dict,
        workspace: Path,
        media_path: Path,
        policy: dict[str, int],
        has_vlm: bool,
    ) -> tuple[Path, str, subprocess.Popen[str]]:
        node_id = manifest["task"]["node_id"]
        report = workspace / f"{node_id}.replay.pb"
        pipeline = workspace / "runtime-pipeline.yaml"
        pipeline.write_text(_runtime_pipeline(policy, has_vlm=has_vlm), encoding="utf-8")
        listen = f"127.0.0.1:{_free_loopback_port()}"
        command = [
            str(_runtime_binary()),
            "replay",
            str(pipeline),
            str(media_path),
            "--report",
            str(report),
            "--sampling-min-interval-ms",
            str(policy["sample_interval_ms"]),
            "--audio-segment-ms",
            str(policy["audio_segment_ms"]),
            "--audio-overlap-ms",
            str(policy["audio_overlap_ms"]),
            "--handoff-listen",
            listen,
            "--handoff-retained-limit",
            "32",
            "--handoff-arena-bytes",
            str(256 * 1024 * 1024),
            "--handoff-ttl-ms",
            "30000",
            "--handoff-wait-timeout-ms",
            "30000",
            "--handoff-idle-timeout-ms",
            "60000",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + RUNTIME_WAIT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TaskExecutionError("runtime_replay_failed")
            channel = grpc.insecure_channel(listen)
            try:
                grpc.channel_ready_future(channel).result(timeout=0.25)
                break
            except grpc.FutureTimeoutError:
                time.sleep(0.1)
            finally:
                channel.close()
        else:
            process.kill()
            process.wait(timeout=5)
            raise TaskExecutionError("runtime_handoff_start_timeout", retryable=True)
        # 生产者会在所有 lease 释放后的 idle timeout 自己退出。调用方仍必须 wait/终止，
        # 防止异常路径留下 Runtime 子进程或占用私有 loopback 端口。
        return report, listen, process

    @staticmethod
    def _stop_replay(process: subprocess.Popen[str]) -> None:
        """等待 Runtime 自然退出，超时后受控终止，不遗留跨任务的 handoff 服务。"""
        if process.poll() is not None:
            process.wait(timeout=1)
            return
        try:
            process.wait(timeout=RUNTIME_WAIT_S)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    @staticmethod
    def _discard_buffers(handoff_endpoint: str, buffers: list) -> None:
        """释放本次 replay 未交给插件的描述符，避免保留表阻塞下一条任务。"""
        reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
        try:
            for entry in buffers:
                try:
                    reader.discard(entry.buffer_id)
                except Exception:  # noqa: BLE001 - 已被插件释放同样是允许的结束状态
                    continue
        finally:
            reader.close()

    def _process_buffers(
        self,
        manifest: dict,
        endpoint: PluginEndpoint,
        source_id: str,
        buffers: list,
        all_buffers: list,
        handoff_endpoint: str,
    ) -> tuple[list[dict], list[dict], str]:
        plugin_channel = grpc.insecure_channel(endpoint.endpoint)
        plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(plugin_channel)
        task = manifest["task"]
        observations: list[dict] = []
        frames: list[dict] = []
        failure = ""
        try:
            for entry in buffers:
                descriptor = common.BufferDescriptor(
                    buffer_id=entry.buffer_id,
                    kind=entry.kind,
                    memory_kind="cpu_shared_memory",
                    locator=common.BufferLocator(
                        offset=entry.offset_bytes,
                        length=entry.length_bytes,
                        handoff_endpoint=handoff_endpoint,
                    ),
                    format=entry.format,
                    stream_id=entry.stream_id,
                    time_range=entry.time_range,
                    content_hash=entry.content_hash,
                )
                request = runtime_pb2.ProcessRequest(
                    context=common.RequestContext(
                        request_id=f"req-{task['task_id']}-{entry.buffer_id}",
                        trace_id=f"execution:{manifest['execution_id']}",
                        pipeline_run_id=manifest["run"]["run_id"],
                        stream_id=entry.stream_id,
                        source_id=source_id,
                        deadline_unix_ms=min(
                            int(task["deadline_unix_ms"]),
                            _now_ms() + int(manifest["plugin"]["deadline_ms"]),
                        ),
                        attempt=int(task["attempt"]),
                        idempotency_key="sha256:"
                        + hashlib.sha256(
                            f"{task['task_id']}|{entry.buffer_id}|{entry.content_hash}".encode()
                        ).hexdigest(),
                        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
                    ),
                    inputs=[runtime_pb2.PluginInput(buffer=descriptor)],
                    processor_release_id=manifest["plugin"]["plugin_version"],
                )
                frame = {
                    "buffer_id": entry.buffer_id,
                    "kind": entry.kind,
                    "source_digest": entry.content_hash,
                    "source_time_range_ms": [entry.time_range.start_ms, entry.time_range.end_ms],
                }
                try:
                    response = plugin.Process(
                        request,
                        timeout=max(1, manifest["plugin"]["deadline_ms"] / 1000),
                    )
                except grpc.RpcError as error:
                    failure = f"plugin_rpc_failed:{error.code().name}"
                    frame["error"] = {"reason": failure, "retryable": True}
                    frames.append(frame)
                    break
                if response.HasField("error"):
                    code = response.error.reason_code or "plugin_processing_failed"
                    failure = ("plugin_retryable:" if response.error.retryable else "") + code
                    frame["error"] = {"reason": code, "retryable": bool(response.error.retryable)}
                    frames.append(frame)
                    break
                if not response.observations:
                    failure = "empty_plugin_result"
                    frame["error"] = {"reason": failure, "retryable": False}
                    frames.append(frame)
                    break
                for observation in response.observations:
                    observations.append(
                        json_format.MessageToDict(observation, preserving_proto_field_name=False)
                    )
                    frames.append({**frame, "observation_id": observation.observation_id})
        finally:
            # 当前任务无需的 buffer 也要显式释放，不让 Runtime 因未消费的保留项滞留。
            try:
                reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
                try:
                    processed = {frame["buffer_id"] for frame in frames}
                    for entry in all_buffers:
                        if entry.buffer_id not in processed:
                            try:
                                reader.discard(entry.buffer_id)
                            except Exception:  # noqa: BLE001 - 插件可能已释放该 lease
                                continue
                finally:
                    reader.close()
            finally:
                plugin_channel.close()
        return observations, frames, failure

    def _write_worker_report(
        self,
        workspace: Path,
        manifest: dict,
        replay_report: Path,
        observations: list[dict],
        frames: list[dict],
        *,
        input_count: int,
        reason: str,
    ) -> tuple[int, int, str]:
        node_id = manifest["task"]["node_id"]
        path = workspace / f"{node_id}.worker.json"
        document = {
            "input_mode": "buffer",
            "failures": [reason] if reason else [],
            "observations": observations,
            "frames": frames,
            "task_summary": {
                "input_count": input_count,
                "output_count": len(observations),
                "reason": reason,
            },
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path.write_bytes(encoded)
        return input_count, len(observations), "agent-result:" + hashlib.sha256(encoded).hexdigest()

    def _run_timeline(
        self, manifest: dict, workspace: Path, media_path: Path
    ) -> tuple[int, int, str]:
        policy = _bounded_policy(manifest["policy"])
        reports = sorted(workspace.glob("*_fast.worker.json")) + sorted(
            workspace.glob("vlm_enrich.worker.json")
        )
        replay_reports = sorted(workspace.glob("*_fast.replay.pb")) + sorted(
            workspace.glob("vlm_enrich.replay.pb")
        )
        if not replay_reports:
            raise TaskExecutionError("timeline_upstream_runtime_report_missing")
        observations: list[dict] = []
        frames: list[dict] = []
        summaries: dict[str, dict] = {}
        for path in reports:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise TaskExecutionError("timeline_worker_report_unreadable") from None
            observations.extend(document.get("observations") or [])
            frames.extend(document.get("frames") or [])
            summaries[path.name.removesuffix(".worker.json")] = document.get("task_summary") or {}
        source = media_pb2.ReplayReport()
        try:
            source.ParseFromString(replay_reports[0].read_bytes())
        except OSError as error:
            raise TaskExecutionError("timeline_replay_report_unreadable") from error
        coverage = self._coverage(source, observations, summaries, policy)
        if not observations:
            self._ingest_timeline(manifest, source, [], [], coverage)
            return 0, 0, "timeline-empty:" + _sha256_json({"coverage": coverage})
        merged = workspace / "timeline.worker.json"
        merged.write_text(
            json.dumps(
                {
                    "input_mode": "buffer",
                    "failures": [],
                    "observations": observations,
                    "frames": frames,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        pipeline = workspace / "runtime-pipeline.yaml"
        pipeline.write_text(
            _runtime_pipeline(policy, has_vlm="vlm_enrich" in summaries), encoding="utf-8"
        )
        material_dir = workspace / "materials"
        timeline_report = workspace / "timeline.json"
        completed = subprocess.run(
            [
                str(_runtime_binary()),
                "timeline",
                str(pipeline),
                str(media_path),
                "--report",
                str(replay_reports[0]),
                "--worker-report",
                str(merged),
                "--material-dir",
                str(material_dir),
                "--out",
                str(timeline_report),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RUNTIME_WAIT_S,
            check=False,
        )
        if completed.returncode != 0 or not timeline_report.is_file():
            raise TaskExecutionError("timeline_fusion_failed")
        try:
            document = json.loads(timeline_report.read_text(encoding="utf-8"))
            items = document["items"]
            unit_bytes = [path.read_bytes() for path in sorted(material_dir.glob("*.material.pb"))]
        except (OSError, KeyError, json.JSONDecodeError):
            raise TaskExecutionError("timeline_output_unreadable") from None
        self._ingest_timeline(manifest, source, unit_bytes, items, coverage)
        return (
            len(observations),
            len(unit_bytes),
            "timeline:" + hashlib.sha256(timeline_report.read_bytes()).hexdigest(),
        )

    def _coverage(
        self,
        source,
        observations: list[dict],
        summaries: dict[str, dict],
        policy: dict[str, int],
    ) -> list[dict]:
        duration = int(source.source.duration_ms)
        if duration <= 0:
            raise TaskExecutionError("timeline_duration_unknown")
        by_modality: dict[str, list[tuple[int, int]]] = {}
        for item in observations:
            time_range = item.get("timeRange") or {}
            try:
                start, end = int(time_range.get("startMs", 0)), int(time_range["endMs"])
            except (KeyError, TypeError, ValueError):
                continue
            by_modality.setdefault(str(item.get("modality", "")), []).append((start, end))
        has_audio = any(track.track_kind == "audio" for track in source.source.tracks)
        has_video = any(track.track_kind == "video" for track in source.source.tracks)
        coverage = []
        for start in range(0, duration, policy["window_ms"]):
            end = min(start + policy["window_ms"], duration)
            states: dict[str, str] = {}
            reasons: dict[str, str] = {}
            for name, modality, task_name, default in (
                ("ocr", "ocr_blocks", "ocr_fast", "not_sampled_by_policy"),
                ("asr", "asr_segment", "asr_fast", "not_scheduled"),
                ("vlm", "vision.scene_description", "vlm_enrich", "not_sampled_by_policy"),
            ):
                observed = any(
                    item_start < end and item_end > start
                    for item_start, item_end in by_modality.get(modality, [])
                )
                if observed:
                    states[name] = "observed"
                elif name == "asr" and not has_audio:
                    states[name], reasons[name] = "not_applicable", "no_audio_track"
                elif name == "ocr" and not has_video:
                    states[name], reasons[name] = "not_applicable", "no_video_track"
                elif summaries.get(task_name, {}).get("reason"):
                    states[name] = "failed"
                    reasons[name] = str(summaries[task_name]["reason"])[:160]
                else:
                    states[name] = default
            sampling = "sampled" if "observed" in states.values() else "not_sampled_by_policy"
            if not has_audio and not has_video:
                sampling = "not_applicable"
            coverage.append(
                {
                    "start_ms": start,
                    "end_ms": end,
                    "sampling_state": sampling,
                    "modality_states": states,
                    "reason_codes": reasons,
                }
            )
        return coverage

    def _ingest_timeline(
        self,
        manifest: dict,
        source,
        units: list[bytes],
        items: list[dict],
        coverage: list[dict],
    ) -> None:
        payload = {
            "intent_id": manifest["intent_id"],
            "source_description_b64": base64.b64encode(
                source.source.SerializeToString(deterministic=True)
            ).decode(),
            "materials_b64": [base64.b64encode(unit).decode() for unit in units],
            "timeline_items": items,
            "coverage": coverage,
        }
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > RESULT_BODY_LIMIT_HINT:
            raise TaskExecutionError("timeline_ingest_payload_too_large")
        try:
            self.client.ingest_task_timeline(manifest["task"]["task_id"], payload)
        except RuntimeError as error:
            code = str(error).split(": ", 1)[-1]
            raise TaskExecutionError(f"timeline_ingest_failed:{code}") from None

    def _report_result(
        self,
        manifest: dict,
        *,
        intent_id: str,
        success: bool,
        retryable: bool,
        reason: str,
        inputs: int,
        outputs: int,
        started_ms: int,
        completed_ms: int,
        result_ref: str,
    ) -> None:
        receipt = _receipt(
            manifest,
            started_ms=started_ms,
            completed_ms=completed_ms,
            inputs=inputs,
            outputs=outputs,
            reason=reason,
            result_ref=result_ref,
        )
        task = manifest["task"]
        self.client.report_task_result(
            task["task_id"],
            {
                "intent_id": intent_id,
                "run_id": manifest["run"]["run_id"],
                "attempt": task["attempt"],
                "assignment_id": task["assignment_id"],
                "success": success,
                "output_ref": result_ref,
                "retryable": retryable,
                "reason_code": reason,
                "error_detail": "",
                "receipt": receipt,
            },
        )
