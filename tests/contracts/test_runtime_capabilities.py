"""Contract checks for the runtime capability report.

These assertions cover the generated contract only; the live behaviour is exercised by
`make runtime-smoke`, which talks to a real Rust process.
"""

import grpc
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub


def test_runtime_service_exposes_capability_report():
    # Channel creation is lazy, so this inspects the generated surface without dialing out.
    with grpc.insecure_channel("127.0.0.1:1") as channel:
        methods = {name for name in dir(RuntimeServiceStub(channel)) if not name.startswith("_")}
    assert {"Health", "DescribeCapabilities"} <= methods


def test_capability_states_are_explicit():
    assert runtime.CapabilityState.Value("CAPABILITY_STATE_UNSPECIFIED") == 0
    assert runtime.CapabilityState.Value("CAPABILITY_STATE_AVAILABLE") == 1
    assert runtime.CapabilityState.Value("CAPABILITY_STATE_UNAVAILABLE") == 2


def test_unavailable_backend_must_carry_a_reason_and_unknowns():
    response = runtime.DescribeCapabilitiesResponse(
        platform="macos-aarch64",
        host=runtime.HostResources(logical_cores=0),
        backends=[
            runtime.BackendCapability(
                backend="coreml",
                platform="macos-aarch64",
                state=runtime.CAPABILITY_STATE_UNAVAILABLE,
                unavailable_reason="execution_backend_not_implemented",
            )
        ],
        admitted_memory_kinds=["cpu_shared_memory", "unified_memory"],
    )
    backend = response.backends[0]
    assert backend.state == runtime.CAPABILITY_STATE_UNAVAILABLE
    assert backend.unavailable_reason
    # Unknown inventory and capacity stay zero instead of defaulting to a guessed value.
    assert response.host.total_memory_bytes == 0
    assert response.host.unified_memory_bytes == 0
    assert backend.max_concurrency == 0
    assert not backend.runtime_version
