"""运行时能力报告的契约校验。

These assertions cover the generated contract only; the live behaviour is exercised by
`make runtime-smoke`, which talks to a real Rust process.
"""

import grpc
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub


def test_runtime_service_exposes_capability_report():
    # channel 创建是延迟的，因此这里只检查生成面，不需要真正发起调用。
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
    # 未知的资源清单与容量保持为 0，而不是使用猜想的默认值。
    assert response.host.total_memory_bytes == 0
    assert response.host.unified_memory_bytes == 0
    assert backend.max_concurrency == 0
    assert not backend.runtime_version
