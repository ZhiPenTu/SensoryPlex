"""自检探针进程：一个只实现生命周期契约的 gRPC 服务，用于热部署状态机验收。

行为由运行目录下的 `mode.json` 选择（执行器把进程的 WorkingDirectory 设成版本化运行目录，
而 mode 文件不在 bundle 摘要范围内，所以**同一份 release 制品**能稳定地走出成功与各类失败路径）：

- `ok`：正常 Describe/ValidateConfig/Start/Health=ready/Drain/Stop；
- `wrong_digest` / `wrong_identity`：Describe 回声与制品不符，验证候选身份核对；
- `invalid_config`：ValidateConfig 判定配置非法；
- `start_fail`：Start 明确失败；
- `health_not_ready`：Start 成功但 Health 永远不到 ready；
- `no_endpoint`：绑定成功却不写 endpoint 文件（验证"不从 stdout 推断端口"）；
- `endpoint_garbage`：写出格式非法的 endpoint 文件；
- `hung_drain`：Drain 挂住不返回，验证排空超时与强制卸载。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import time

import grpc
from edge_material_sdk import remove_endpoint_file, write_endpoint_file
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format

from .artifact import package_digest
from .plugin import PLUGIN_ID, PLUGIN_VERSION, describe, validate_config

MODE_FILE = "mode.json"
VALID_MODES = {
    "ok",
    "wrong_digest",
    "wrong_identity",
    "invalid_config",
    "start_fail",
    "health_not_ready",
    "no_endpoint",
    "endpoint_garbage",
    "hung_drain",
}


def current_mode() -> str:
    path = pathlib.Path(MODE_FILE)
    if not path.is_file():
        return "ok"
    try:
        mode = str(json.loads(path.read_text()).get("mode", "ok"))
    except ValueError:
        return "ok"
    if mode not in VALID_MODES:
        print(f"canary: unknown mode {mode}", flush=True)
        return "ok"
    return mode


def _failure(reason: str):
    return common.ProcessingError(code=common.INVALID_INPUT, reason_code=reason, retryable=False)


class CanaryServicer(runtime_pb2_grpc.ProcessorPluginServiceServicer):
    def __init__(self, artifact_digest: str, mode: str):
        self.artifact_digest = artifact_digest
        self.mode = mode
        self.state = "created"

    def Describe(self, request, context):
        if self.mode == "wrong_digest":
            return describe("sha256:" + "0" * 64)
        if self.mode == "wrong_identity":
            return describe(self.artifact_digest, plugin_id="org.sensoryplex.not-the-candidate")
        return describe(self.artifact_digest)

    def ValidateConfig(self, request, context):
        if self.mode == "invalid_config":
            return runtime_pb2.ValidationResult(valid=False, field_errors=["canary_invalid_config"])
        return validate_config(json_format.MessageToDict(request.config))

    def Health(self, request, context):
        state = "starting" if self.mode == "health_not_ready" else self.state
        unavailable = [] if state == "ready" else ["plugin_not_started"]
        return runtime_pb2.HealthResponse(state=state, unavailable_capabilities=unavailable)

    def Drain(self, request, context):
        if self.mode == "hung_drain":
            time.sleep(int(request.grace_period_ms or 1000) / 1000.0 + 60.0)
        if self.state == "ready":
            self.state = "draining"
        return runtime_pb2.LifecycleResponse(state=self.state)

    def Stop(self, request, context):
        self.state = "stopped"
        return runtime_pb2.LifecycleResponse(state=self.state)

    def Start(self, request, context):
        if self.mode == "start_fail":
            self.state = "failed"
            return runtime_pb2.LifecycleResponse(
                state="failed", error=_failure("canary_start_fail")
            )
        self.state = "ready"
        return runtime_pb2.LifecycleResponse(state="ready")

    def Process(self, request, context):
        return runtime_pb2.ProcessResponse(error=_failure("canary_has_no_business_data"))

    def Cancel(self, request, context):
        return runtime_pb2.LifecycleResponse(state=self.state)


def serve(port: int, expect_digest: str | None = None, endpoint_file: str | None = None) -> int:
    digest = package_digest()
    if expect_digest and expect_digest != digest:
        print(f"artifact digest mismatch: expected {expect_digest} got {digest}", flush=True)
        return 2
    mode = current_mode()
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=4))
    runtime_pb2_grpc.add_ProcessorPluginServiceServicer_to_server(
        CanaryServicer(digest, mode), server
    )
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    if bound == 0:
        print(f"failed to bind 127.0.0.1:{port}", flush=True)
        return 2
    server.start()
    if endpoint_file and mode == "endpoint_garbage":
        pathlib.Path(endpoint_file).write_text('{"format": "not-the-endpoint-contract"}\n')
    elif endpoint_file and mode != "no_endpoint":
        write_endpoint_file(
            endpoint_file,
            f"127.0.0.1:{bound}",
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
            artifact_digest=digest,
        )
    print(f"canary ready mode={mode} port={bound} artifact_digest={digest}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=1.0)
    finally:
        remove_endpoint_file(endpoint_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SensoryPlex deploy canary plugin")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expect-digest", default=None)
    parser.add_argument("--endpoint-file", default=None)
    arguments = parser.parse_args(argv)
    return serve(arguments.port, arguments.expect_digest, arguments.endpoint_file)
