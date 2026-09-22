"""Call the actual Rust gRPC process with the generated Python client."""

import os
import signal
import socket
import subprocess
from pathlib import Path

import grpc
from edge_material_sdk.generated.runtime.v1.runtime_pb2 import HealthRequest
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub

ROOT = Path(__file__).resolve().parents[1]
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
        response = RuntimeServiceStub(channel).Health(HealthRequest(), timeout=3)
        assert response.state == "degraded"
        assert "media_ingestion" in response.unavailable_capabilities
        print(
            "Rust server / Python gRPC client: PASS; unavailable capabilities reported explicitly"
        )
finally:
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
