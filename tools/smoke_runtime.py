"""Call the actual Rust gRPC process with the generated Python client."""

import os
import platform as host_platform
import signal
import socket
import subprocess
from pathlib import Path

import grpc
from edge_material_sdk.generated.runtime.v1.runtime_pb2 import (
    CAPABILITY_STATE_UNAVAILABLE,
    DescribeCapabilitiesRequest,
    HealthRequest,
)
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub

ROOT = Path(__file__).resolve().parents[1]

# Rust reports `std::env::consts`, which spells Apple Silicon as macos-aarch64.
SYSTEMS = {"darwin": "macos", "linux": "linux"}
ARCHITECTURES = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}


def expected_platform():
    system = host_platform.system().lower()
    machine = host_platform.machine().lower()
    return f"{SYSTEMS.get(system, system)}-{ARCHITECTURES.get(machine, machine)}"


with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
address = f"127.0.0.1:{port}"
environment = os.environ | {"SENSORYPLEX_RUNTIME_ADDR": address}
process = subprocess.Popen(
    [str(ROOT / "target/debug/sensoryplex-runtime"), "serve"], env=environment
)
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
        print(
            f"Rust server / Python gRPC client: PASS; platform={capabilities.platform}; "
            "unavailable backends and capabilities reported explicitly"
        )
finally:
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
