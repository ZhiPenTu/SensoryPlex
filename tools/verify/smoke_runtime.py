"""使用生成的 Python 客户端调用真实的 Rust gRPC 进程。

被验收的进程是**主机构建的原生二进制**（macOS 上是 Mach-O），所以本脚本只在主机侧
执行（Makefile 的 `runtime-smoke` 用 `PY_HOST`）；放进 Linux 容器里连 exec 都过不去。
"""

import argparse
import os
import platform as host_platform
import signal
import socket
import subprocess
from pathlib import Path

import grpc
from edge_material_sdk.generated.runtime.v1.runtime_pb2 import (
    ACCELERATOR_STATE_AVAILABLE,
    ACCELERATOR_STATE_UNSPECIFIED,
    CAPABILITY_STATE_UNAVAILABLE,
    DescribeCapabilitiesRequest,
    HealthRequest,
)
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub

ROOT = Path(__file__).resolve().parents[2]

# Rust 使用 `std::env::consts`，其中 Apple Silicon 写作 macos-aarch64。
SYSTEMS = {"darwin": "macos", "linux": "linux"}
ARCHITECTURES = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}

# 与 crates/runtime/src/accelerator.rs 的 `expected()` 同源：本目标上预期存在的加速器。
EXPECTED_ACCELERATORS = {
    "macos-aarch64": ("coreml", "metal"),
    "linux-x86_64": ("cuda",),
}


def expected_platform():
    system = host_platform.system().lower()
    machine = host_platform.machine().lower()
    return f"{SYSTEMS.get(system, system)}-{ARCHITECTURES.get(machine, machine)}"


def start_runtime(binary, address):
    try:
        return subprocess.Popen(
            [str(binary), "serve"],
            env=os.environ | {"SENSORYPLEX_RUNTIME_ADDR": address},
        )
    except OSError as error:
        raise SystemExit(
            f"无法启动 {binary}: {error}\n"
            "sensoryplex-runtime 是与构建主机同架构的原生二进制，必须在能运行它的机器上验收"
            "（本机默认走 `make runtime-smoke`，它使用主机侧 PY_HOST）。"
        ) from error


def assert_accelerators(capabilities):
    """宿主加速器表：三态不得塌陷，且与执行后端互不覆盖（ADR-022）。"""
    platform = capabilities.platform
    facts = list(capabilities.host_accelerators)
    expected = EXPECTED_ACCELERATORS.get(platform, ())
    assert [fact.accelerator for fact in facts] == list(expected), (
        f"{platform} 上预期 {expected}，实际 {[fact.accelerator for fact in facts]}"
    )
    for fact in facts:
        assert fact.state != ACCELERATOR_STATE_UNSPECIFIED, f"{fact.accelerator} 不能留 unspecified"
        assert fact.platform == platform
        assert fact.detection_source, f"{fact.accelerator} 必须写明探测来源"
        available = fact.state == ACCELERATOR_STATE_AVAILABLE
        reason = fact.unavailable_reason
        assert bool(reason) != available, (
            f"{fact.accelerator} 的原因串与状态不匹配：state={fact.state} reason={reason!r}"
        )
        if available:
            assert fact.runtime_version, f"{fact.accelerator} 报 available 就必须带上真读到的版本"
        for item in fact.evidence:
            assert "/" not in item, f"evidence 不得携带路径: {item}"
        # 宿主有加速器，不等于本进程能用它执行推理。
        for backend in capabilities.backends:
            assert backend.state == CAPABILITY_STATE_UNAVAILABLE, backend.backend
    return facts


parser = argparse.ArgumentParser(description="对真实 Rust gRPC 进程做运行时能力 smoke")
parser.add_argument("--binary", default=str(ROOT / "target/debug/sensoryplex-runtime"))
args = parser.parse_args()

binary = Path(args.binary)
if not binary.exists():
    raise SystemExit(
        f"{binary} 不存在；先运行 `cargo build -p sensoryplex-runtime`（或 make runtime-smoke）"
    )

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
address = f"127.0.0.1:{port}"
process = start_runtime(binary, address)
try:
    with grpc.insecure_channel(address) as channel:
        grpc.channel_ready_future(channel).result(timeout=10)
        stub = RuntimeServiceStub(channel)
        response = stub.Health(HealthRequest(), timeout=3)
        assert response.state == "degraded"
        assert "media_ingestion" in response.unavailable_capabilities
        capabilities = stub.DescribeCapabilities(DescribeCapabilitiesRequest(), timeout=3)
        assert capabilities.platform == expected_platform(), capabilities.platform
        assert sorted(capabilities.unavailable_capabilities) == sorted(
            response.unavailable_capabilities
        )
        assert capabilities.host.total_memory_bytes > 0
        assert "cpu_shared_memory" in capabilities.admitted_memory_kinds
        apple_silicon = capabilities.platform == "macos-aarch64"
        assert ("unified_memory" in capabilities.admitted_memory_kinds) == apple_silicon
        if apple_silicon:
            assert capabilities.host.unified_memory_bytes > 0
        else:
            assert capabilities.host.unified_memory_bytes == 0
        assert any(backend.backend == "cpu" for backend in capabilities.backends)
        for backend in capabilities.backends:
            assert backend.state == CAPABILITY_STATE_UNAVAILABLE
            assert backend.unavailable_reason, f"{backend.backend} must state a reason"
            assert not backend.runtime_version and backend.max_concurrency == 0
        facts = assert_accelerators(capabilities)

        accelerated = ", ".join(
            f"{fact.accelerator}="
            + (
                f"available({fact.runtime_version})"
                if fact.state == ACCELERATOR_STATE_AVAILABLE
                else f"unavailable({fact.unavailable_reason})"
            )
            for fact in facts
        )
        print(
            f"Rust server / Python gRPC client: PASS; platform={capabilities.platform}; "
            f"host accelerators=[{accelerated or 'none_expected'}]; "
            "unavailable backends and capabilities reported explicitly"
        )
finally:
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
