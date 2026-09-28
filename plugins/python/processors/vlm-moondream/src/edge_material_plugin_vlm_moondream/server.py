"""插件进程：把 `VisionVlmPlugin` 暴露成契约里的 gRPC 生命周期接口。

Runtime/worker 只通过 `runtime.v1.ProcessorPluginService` 与本进程说话；本进程不解析媒体文件、
不知道 pipeline、也不接受"绕过 lease 的字节"。模型服务的可达性在 `Start` 时就**真实探测**：
端口写错不是等到第一次推理才失败，而是起不来。
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import threading

import grpc
from edge_material_sdk import (
    BufferReadError,
    LeaseBufferReader,
    remove_endpoint_file,
    write_endpoint_file,
)
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format

from .artifact import package_digest
from .plugin import VisionVlmPlugin, describe, validate_config


def _failure(code: int, reason: str, retryable: bool = False):
    return common.ProcessingError(code=code, reason_code=reason, retryable=retryable)


class PluginServicer(runtime_pb2_grpc.ProcessorPluginServiceServicer):
    def __init__(self, plugin: VisionVlmPlugin, loop: asyncio.AbstractEventLoop):
        self.plugin = plugin
        self.state = "created"
        self._loop = loop
        self._in_flight: dict[str, concurrent.futures.Future] = {}

    # --- 生命周期 -------------------------------------------------------------
    def Describe(self, request, context):
        return describe(self.plugin.artifact_digest)

    def ValidateConfig(self, request, context):
        return validate_config(json_format.MessageToDict(request.config))

    def Health(self, request, context):
        unavailable = [] if self.state == "ready" else ["plugin_not_started"]
        return runtime_pb2.HealthResponse(state=self.state, unavailable_capabilities=unavailable)

    def Drain(self, request, context):
        if self.state == "ready":
            self.state = "draining"
        return runtime_pb2.LifecycleResponse(state=self.state)

    def Stop(self, request, context):
        self.state = "stopped"
        self.plugin.close()
        return runtime_pb2.LifecycleResponse(state=self.state)

    def Start(self, request, context):
        config = json_format.MessageToDict(request.config)
        if self.state not in {"created", "failed", "stopped"}:
            return runtime_pb2.LifecycleResponse(
                state=self.state, error=_failure(common.INVALID_INPUT, "plugin_already_started")
            )
        validated = validate_config(config)
        if not validated.valid:
            self.state = "failed"
            return runtime_pb2.LifecycleResponse(
                state=self.state, error=_failure(common.INVALID_INPUT, validated.field_errors[0])
            )
        try:
            self.plugin.configure(config)
        except (ValueError, OSError) as error:
            self.state = "failed"
            return runtime_pb2.LifecycleResponse(
                state=self.state, error=_failure(common.TRANSIENT_BACKEND_FAILURE, str(error), True)
            )
        try:
            # 只有 static 在启动时绑定固定数据面；per_request 从 descriptor 领料，
            # local_decode 按受控媒体引用解码，两者都不要求固定 handoff 地址。
            if config.get("data_plane_mode", "static") == "static":
                if self.plugin.buffer_reader is None:
                    self.plugin.buffer_reader = LeaseBufferReader(
                        config["handoff_endpoint"], ttl_ms=int(config.get("ttl_ms", 30_000))
                    )
                self.plugin.buffer_reader.listing()
        except (BufferReadError, KeyError, ValueError, grpc.RpcError) as error:
            self.state = "failed"
            reason = getattr(error, "reason_code", None) or str(error) or "data_plane_unreachable"
            return runtime_pb2.LifecycleResponse(
                state=self.state, error=_failure(common.TRANSIENT_BACKEND_FAILURE, reason, True)
            )
        self.state = "ready"
        return runtime_pb2.LifecycleResponse(state=self.state)

    # --- 处理 -----------------------------------------------------------------
    def Process(self, request, context):
        if self.state != "ready":
            return runtime_pb2.ProcessResponse(
                error=_failure(common.UNSUPPORTED_CAPABILITY, f"plugin_not_ready:{self.state}")
            )
        future = asyncio.run_coroutine_threadsafe(self.plugin.invoke(request), self._loop)
        self._in_flight[request.context.request_id] = future
        try:
            return future.result()
        except concurrent.futures.CancelledError:
            return runtime_pb2.ProcessResponse(
                error=_failure(common.DEADLINE_EXCEEDED, "processing_cancelled", True)
            )
        finally:
            self._in_flight.pop(request.context.request_id, None)

    def Cancel(self, request, context):
        future = self._in_flight.get(request.request_id)
        if future is None:
            return runtime_pb2.LifecycleResponse(
                state=self.state, error=_failure(common.INVALID_INPUT, "unknown_request")
            )
        future.cancel()
        return runtime_pb2.LifecycleResponse(state=self.state)


def build_server(plugin: VisionVlmPlugin, loop: asyncio.AbstractEventLoop, workers: int = 4):
    server = grpc.server(
        concurrent.futures.ThreadPoolExecutor(max_workers=workers),
        options=[("grpc.so_reuseport", 0)],
    )
    runtime_pb2_grpc.add_ProcessorPluginServiceServicer_to_server(
        PluginServicer(plugin, loop), server
    )
    return server


def serve(port: int, expect_digest: str | None = None, endpoint_file: str | None = None) -> int:
    digest = package_digest()
    if expect_digest and expect_digest != digest:
        # 摘要漂移不是警告：manifest 里写的摘要必须与当前代码一致，否则拒绝启动。
        print(f"artifact digest mismatch: expected {expect_digest} got {digest}")
        return 2
    description = describe(digest)
    plugin = VisionVlmPlugin(artifact_digest=digest)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = build_server(plugin, loop)
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    if bound == 0:
        print(f"failed to bind 127.0.0.1:{port}")
        return 2
    server.start()
    # endpoint 文件在绑定成功之后、对外宣告就绪之前原子写出（ADR-030）。
    if endpoint_file:
        write_endpoint_file(
            endpoint_file,
            f"127.0.0.1:{bound}",
            plugin_id=description.name,
            plugin_version=description.version,
            artifact_digest=digest,
        )
    print(f"plugin ready name={description.name} port={bound} artifact_digest={digest}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=1.0)
    finally:
        # 进程退出即删掉 endpoint 文件：留着它会让执行器解析到死端点。
        remove_endpoint_file(endpoint_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SensoryPlex VLM processor plugin (gRPC)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expect-digest", default=None)
    parser.add_argument("--endpoint-file", default=None)
    arguments = parser.parse_args(argv)
    return serve(arguments.port, arguments.expect_digest, arguments.endpoint_file)
