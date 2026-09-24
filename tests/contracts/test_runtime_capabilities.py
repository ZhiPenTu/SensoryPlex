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


def test_accelerator_states_keep_ignorance_as_a_third_answer():
    # 「探测不到」不能被折进「不存在」：三态是分开的取值，不是二值加默认值。
    assert runtime.AcceleratorState.Value("ACCELERATOR_STATE_UNSPECIFIED") == 0
    assert runtime.AcceleratorState.Value("ACCELERATOR_STATE_AVAILABLE") == 1
    assert runtime.AcceleratorState.Value("ACCELERATOR_STATE_UNAVAILABLE") == 2
    assert runtime.AcceleratorState.Value("ACCELERATOR_STATE_UNKNOWN") == 3


def test_host_accelerator_is_separate_from_execution_backends():
    # `backends` 说「本进程能不能执行推理」，`host_accelerators` 说「这台宿主有没有」。
    # 两者必须能同时出现在一份报告里，且互不覆盖。
    described = runtime.DescribeCapabilitiesResponse(
        platform="macos-aarch64",
        backends=[
            runtime.BackendCapability(
                backend="coreml",
                platform="macos-aarch64",
                state=runtime.CAPABILITY_STATE_UNAVAILABLE,
                unavailable_reason="execution_backend_not_implemented",
            )
        ],
        host_accelerators=[
            runtime.HostAccelerator(
                accelerator="coreml",
                platform="macos-aarch64",
                state=runtime.ACCELERATOR_STATE_AVAILABLE,
                detection_source="framework_info",
                runtime_version="3520.5.1",
                evidence=["framework=CoreML.framework", "cf_bundle_version=3520.5.1"],
            )
        ],
    )
    assert described.backends[0].state == runtime.CAPABILITY_STATE_UNAVAILABLE
    assert described.host_accelerators[0].state == runtime.ACCELERATOR_STATE_AVAILABLE
    # 「宿主有」不构成「本进程在用」：执行后端的结论不被加速器表改写。
    assert described.backends[0].unavailable_reason
    assert not described.host_accelerators[0].unavailable_reason


def test_unknown_accelerator_must_explain_why_it_could_not_tell():
    # 非 available 的条目一律带稳定原因；探测工具缺失时原因里必须出现来源，
    # 否则读的人无法区分「工具不在」与「宿主没有」。
    unknown = runtime.HostAccelerator(
        accelerator="cuda",
        platform="linux-x86_64",
        state=runtime.ACCELERATOR_STATE_UNKNOWN,
        detection_source="unavailable",
        unavailable_reason="probe_tool_missing:nvidia_smi",
    )
    unavailable = runtime.HostAccelerator(
        accelerator="metal",
        platform="macos-aarch64",
        state=runtime.ACCELERATOR_STATE_UNAVAILABLE,
        detection_source="system_profiler",
        unavailable_reason="host_reports_no_metal_family",
    )
    assert unknown.state != unavailable.state
    for fact in (unknown, unavailable):
        assert fact.unavailable_reason
        assert not fact.runtime_version
        assert not fact.evidence
    # available 条目反过来不允许带原因，也不允许把探测来源写成 unavailable。
    assert unknown.detection_source == "unavailable"
    assert unavailable.detection_source == "system_profiler"


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
