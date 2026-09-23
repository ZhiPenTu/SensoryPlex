"""模型 worker（调度侧）：发现数据面里的输入（帧或音频段），按契约调用插件，收集 observation。

三个角色必须分开，否则"跨进程"只是口号：
1. Runtime（生产者，持有字节与保留表）；
2. 本进程（worker：只做发现、构造请求、调用插件、对账）；
3. 插件进程（`edge_material_plugin_vlm_moondream`：自己按 lease 读字节、跑模型）。

本进程**不读**任何输入字节：它从 `List` 只拿到不透明句柄、区间与摘要；真正的字节由插件
通过 `BufferHandoffService.Acquire` 领取。因此 worker 的日志与报告里不可能出现原始媒体。

一个 worker 服务所有插件：`--input-kind` 决定它挑哪种 buffer，`--plugin-config` 把插件自己的
Start 配置整份传下去，而不是让 worker 猜每个插件要什么键。
"""

import argparse
import hashlib
import json
import os
import pathlib
import socket
import sys
import time

import grpc
from edge_material_sdk import BufferReadError, LeaseBufferReader
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.media.v1 import handoff_pb2, handoff_pb2_grpc
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format, struct_pb2

DEFAULT_DEADLINE_MS = 180_000
DEFAULT_TIMEOUT_S = 300.0


def as_struct(config: dict) -> struct_pb2.Struct:
    return json_format.ParseDict(config, struct_pb2.Struct())


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def to_descriptor(entry) -> common.BufferDescriptor:
    """把清单里的一条保留项变成插件输入。字节不在消息里，只有句柄、区间与摘要。"""
    return common.BufferDescriptor(
        buffer_id=entry.buffer_id,
        kind=entry.kind,
        memory_kind="cpu_shared_memory",
        locator=common.BufferLocator(offset=entry.offset_bytes, length=entry.length_bytes),
        format=entry.format,
        stream_id=entry.stream_id,
        time_range=entry.time_range,
        content_hash=entry.content_hash,
    )


def make_request(entry, source_id: str, run_id: str, release_id: str, deadline_ms: int):
    descriptor = to_descriptor(entry)
    context = common.RequestContext(
        request_id=f"req_{stable_id(entry.buffer_id, entry.content_hash)}",
        trace_id=f"trace_{run_id}",
        pipeline_run_id=run_id,
        stream_id=entry.stream_id,
        source_id=source_id,
        deadline_unix_ms=int(time.time() * 1000) + deadline_ms,
        attempt=1,
        idempotency_key=stable_id(entry.buffer_id, entry.content_hash),
        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
    )
    return runtime_pb2.ProcessRequest(
        context=context,
        inputs=[runtime_pb2.PluginInput(buffer=descriptor)],
        processor_release_id=release_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-plane", required=True, help="Runtime 数据面 gRPC 地址")
    parser.add_argument("--plugin", required=True, help="插件 gRPC 地址")
    parser.add_argument("--source-id", default="source_local_file")
    parser.add_argument("--model-endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="moondream:v2")
    parser.add_argument(
        "--input-kind",
        default="video_frame",
        help="要处理的 buffer 种类（video_frame / audio_segment）",
    )
    parser.add_argument(
        "--plugin-config",
        default=None,
        help="插件 Start 配置的 JSON 文件；给出时替代 --model-endpoint/--model",
    )
    parser.add_argument("--max-inputs", type=int, default=None, help="最多处理多少个输入")
    parser.add_argument("--max-frames", type=int, default=2, help="--max-inputs 的兼容别名")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args()
    max_inputs = arguments.max_inputs if arguments.max_inputs is not None else arguments.max_frames

    report = {
        "worker_pid": os.getpid(),
        "worker_host": socket.gethostname(),
        "data_plane": arguments.data_plane,
        "plugin": arguments.plugin,
        "input_kind": arguments.input_kind,
        "observations": [],
        # 处理过的输入清单（视频帧或音频段）。保留 `frames` 这个键名，验收脚本依赖它。
        "frames": [],
        "failures": [],
    }

    data_channel = grpc.insecure_channel(arguments.data_plane)
    handoff = handoff_pb2_grpc.BufferHandoffServiceStub(data_channel)
    reader = LeaseBufferReader(arguments.data_plane, ttl_ms=30_000, timeout_s=arguments.timeout_s)
    plugin_channel = grpc.insecure_channel(arguments.plugin)
    plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(plugin_channel)

    def fail(reason: str) -> int:
        report["failures"].append(reason)
        pathlib.Path(arguments.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(f"ai worker failed: {reason}")
        return 1

    try:
        description = plugin.Describe(runtime_pb2.DescribeRequest(), timeout=arguments.timeout_s)
    except grpc.RpcError as error:
        return fail(f"plugin_describe_failed:{error.code().name}")
    report["plugin_description"] = {
        "name": description.name,
        "version": description.version,
        "protocol": description.protocol,
        "consumes": list(description.consumes),
        "produces": list(description.produces),
        "memory_kinds": list(description.memory_kinds),
    }

    start_config = {
        "handoff_endpoint": arguments.data_plane,
        "ttl_ms": 30_000,
        "timeout_s": arguments.timeout_s,
    }
    if arguments.plugin_config:
        # 插件自己的配置整份传下去；数据面地址由 worker 决定，不被文件覆盖。
        try:
            supplied = json.loads(pathlib.Path(arguments.plugin_config).read_text())
        except (OSError, json.JSONDecodeError) as error:
            return fail(f"plugin_config_unreadable:{type(error).__name__}")
        if not isinstance(supplied, dict):
            return fail("plugin_config_not_an_object")
        start_config.update(supplied)
        start_config["handoff_endpoint"] = arguments.data_plane
    else:
        start_config["endpoint"] = arguments.model_endpoint
        start_config["model"] = arguments.model
    validation = plugin.ValidateConfig(
        runtime_pb2.ValidateConfigRequest(config=as_struct(start_config)),
        timeout=arguments.timeout_s,
    )
    report["config_valid"] = validation.valid
    if not validation.valid:
        return fail(f"config_rejected:{list(validation.field_errors)}")

    lifecycle = plugin.Start(
        runtime_pb2.StartRequest(config=as_struct(start_config)), timeout=arguments.timeout_s
    )
    report["start_state"] = lifecycle.state
    if lifecycle.state != "ready":
        return fail(f"plugin_start_failed:{lifecycle.state}:{lifecycle.error.reason_code}")

    try:
        listing = handoff.List(handoff_pb2.ListRetainedRequest(), timeout=arguments.timeout_s)
    except grpc.RpcError as error:
        return fail(f"data_plane_list_failed:{error.code().name}")

    stats = listing.stats
    report["runtime_stats"] = {
        "retained_total": stats.retained_total,
        "retained": stats.retained,
        "released_total": stats.released_total,
        "expired_total": stats.expired_total,
        "arena_live_slabs": stats.arena_live_slabs,
        "retained_by_kind": dict(stats.retained_by_kind),
    }
    report["segment_name_hint"] = "opaque" if listing.segment_name else "missing"

    inputs = [entry for entry in listing.buffers if entry.kind == arguments.input_kind]
    report["input_buffers_available"] = len(inputs)
    if arguments.input_kind == "video_frame":
        # 兼容既有验收脚本的键名。
        report["video_buffers_available"] = len(inputs)
    if not inputs:
        return fail(f"no_{arguments.input_kind}_buffer_to_process")
    # 取"最大输入优先"：同一条流里它最有信息量；仍然只处理有限的 max_inputs 条。
    selected = sorted(inputs, key=lambda entry: entry.length_bytes, reverse=True)[:max_inputs]
    run_id = "run_" + stable_id(arguments.source_id, *[entry.buffer_id for entry in selected])

    for entry in selected:
        request = make_request(
            entry, arguments.source_id, run_id, description.version, DEFAULT_DEADLINE_MS
        )
        started = time.time()
        try:
            response = plugin.Process(request, timeout=arguments.timeout_s)
        except grpc.RpcError as error:
            return fail(f"plugin_process_failed:{error.code().name}")
        elapsed_ms = round((time.time() - started) * 1000, 1)
        if response.HasField("error"):
            report["frames"].append(
                {
                    "buffer_id": entry.buffer_id,
                    "error": {
                        "code": common.ErrorCode.Name(response.error.code),
                        "reason": response.error.reason_code,
                        "retryable": response.error.retryable,
                    },
                    "elapsed_ms": elapsed_ms,
                }
            )
            continue
        for observation in response.observations:
            report["observations"].append(json_format.MessageToDict(observation))
            report["frames"].append(
                {
                    "buffer_id": entry.buffer_id,
                    "kind": entry.kind,
                    "observation_id": observation.observation_id,
                    "time_range_ms": [
                        observation.time_range.start_ms,
                        observation.time_range.end_ms,
                    ],
                    "source_time_range_ms": [entry.time_range.start_ms, entry.time_range.end_ms],
                    "source_digest": entry.content_hash,
                    "observation_digest": observation.content_hash,
                    "elapsed_ms": elapsed_ms,
                }
            )

    # 数据面要求每条保留都有归宿：调度方必须显式确认"已投递、不消费"的 buffer，
    # 不能用自己的过滤规则把剩下的条目留在保留表里（否则运行以账目失败收尾）。
    drain = {"discarded": 0, "failures": []}
    processed_ids = {frame["buffer_id"] for frame in report["frames"]}
    for entry in listing.buffers:
        if entry.buffer_id in processed_ids:
            continue
        try:
            reader.discard(entry.buffer_id)
            drain["discarded"] += 1
        except BufferReadError as error:
            drain["failures"].append({"buffer_id": entry.buffer_id, "reason": error.reason_code})
        except grpc.RpcError as error:
            drain["failures"].append({"buffer_id": entry.buffer_id, "reason": error.code().name})
    report["drain"] = drain

    # 处理器必须自己归还 lease：这里读到的账目是"插件读完就还了"的证据。
    try:
        after = handoff.Stats(handoff_pb2.HandoffStatsRequest(), timeout=arguments.timeout_s)
        report["runtime_stats_after"] = {
            "retained_total": after.stats.retained_total,
            "retained": after.stats.retained,
            "released_total": after.stats.released_total,
            "expired_total": after.stats.expired_total,
            "leased": after.stats.leased,
            "arena_live_slabs": after.stats.arena_live_slabs,
        }
    except grpc.RpcError as error:
        return fail(f"data_plane_stats_failed:{error.code().name}")

    reader.close()
    stopped = plugin.Stop(runtime_pb2.StopRequest(), timeout=arguments.timeout_s)
    report["stop_state"] = stopped.state
    report["checks_failed"] = len(report["failures"])
    pathlib.Path(arguments.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"ai worker ok: observations={len(report['observations'])} "
        f"frames={len(selected)} plugin={description.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
