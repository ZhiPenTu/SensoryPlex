"""W3C TraceContext 与 OpenTelemetry 链路追踪契约测试。"""

import time

from edge_material_sdk.logger import get_trace_id
from edge_material_sdk.telemetry import (
    Span,
    TraceContext,
    TraceRecorder,
    get_trace_context,
    get_tracer,
    set_trace_context,
    trace_context_scope,
    trace_span,
)


def test_w3c_traceparent_generation_and_parsing():
    root = TraceContext.new_root(sampled=True)
    assert len(root.trace_id) == 32
    assert len(root.span_id) == 16
    assert root.parent_span_id is None
    assert root.trace_flags == "01"
    assert root.is_sampled is True
    assert root.traceparent.startswith(f"00-{root.trace_id}-{root.span_id}-01")

    # 解析标准头部并生成下游 child span
    parsed = TraceContext.from_traceparent(root.traceparent)
    assert parsed is not None
    assert parsed.trace_id == root.trace_id
    assert parsed.parent_span_id == root.span_id
    assert parsed.span_id != root.span_id  # 派生新 child span_id
    assert parsed.trace_flags == "01"
    assert parsed.is_sampled is True


def test_w3c_traceparent_rejection_rules():
    # 非法格式
    assert TraceContext.from_traceparent("") is None
    assert TraceContext.from_traceparent("invalid") is None
    # 错误版本（目前仅支持 00）
    assert (
        TraceContext.from_traceparent("01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        is None
    )
    # 全 0 trace_id 禁止
    assert (
        TraceContext.from_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01")
        is None
    )
    # 全 0 span_id 禁止
    assert (
        TraceContext.from_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01")
        is None
    )
    # 长度不合规
    assert TraceContext.from_traceparent("00-4bf92f3577b34da6a3ce-00f067aa0ba902b7-01") is None


def test_trace_context_propagation_and_logger_synchronization():
    ctx = TraceContext.new_root()
    set_trace_context(ctx)
    assert get_trace_context() == ctx
    # 必须自动同步 logger 的 trace_id，确保日志与链路标识完全一致
    assert get_trace_id() == ctx.trace_id

    child = ctx.create_child()
    with trace_context_scope(child):
        assert get_trace_context() == child
        assert get_trace_id() == child.trace_id

    assert get_trace_context() == ctx
    assert get_trace_id() == ctx.trace_id
    set_trace_context(None)
    assert get_trace_context() is None
    assert get_trace_id() is None


def test_trace_recorder_waterfall_and_bottleneck_detection():
    recorder = TraceRecorder(max_traces=50)
    trace_id = "a" * 32

    # 构造父子调用的真实链路：
    # api.request (0ms ~ 120ms)
    #   -> worker.task (10ms ~ 110ms, 100ms 耗时，瓶颈)
    #        -> plugin.inference (20ms ~ 90ms, 70ms)
    #   -> outbox.relay (110ms ~ 118ms, 8ms)
    t0 = time.time_ns()
    s_root = Span(
        name="api.request",
        trace_id=trace_id,
        span_id="1" * 16,
        service_name="api",
        start_time_ns=t0,
        end_time_ns=t0 + 120_000_000,
    )
    s_worker = Span(
        name="worker.task",
        trace_id=trace_id,
        span_id="2" * 16,
        parent_span_id="1" * 16,
        service_name="worker",
        start_time_ns=t0 + 10_000_000,
        end_time_ns=t0 + 110_000_000,
    )
    s_plugin = Span(
        name="plugin.inference",
        trace_id=trace_id,
        span_id="3" * 16,
        parent_span_id="2" * 16,
        service_name="plugin",
        start_time_ns=t0 + 20_000_000,
        end_time_ns=t0 + 90_000_000,
    )
    s_relay = Span(
        name="outbox.relay",
        trace_id=trace_id,
        span_id="4" * 16,
        parent_span_id="1" * 16,
        service_name="relay",
        start_time_ns=t0 + 110_000_000,
        end_time_ns=t0 + 118_000_000,
    )

    recorder.record_span(s_root)
    recorder.record_span(s_worker)
    recorder.record_span(s_plugin)
    recorder.record_span(s_relay)

    waterfall = recorder.get_waterfall(trace_id)
    assert waterfall is not None
    assert waterfall["trace_id"] == trace_id
    assert waterfall["span_count"] == 4
    assert waterfall["total_duration_ms"] >= 120.0

    # 瓶颈定位：根虽然最长，但在业务子阶段中 worker.task (100ms) 占据主导
    assert waterfall["bottleneck"] is not None
    assert waterfall["bottleneck"]["span_id"] in ("1" * 16, "2" * 16)

    # 树形层级验证
    roots = waterfall["waterfall"]
    assert len(roots) == 1
    assert roots[0]["name"] == "api.request"
    assert len(roots[0]["children"]) == 2  # worker.task 与 outbox.relay
    worker_node = [c for c in roots[0]["children"] if c["name"] == "worker.task"][0]
    assert len(worker_node["children"]) == 1
    assert worker_node["children"][0]["name"] == "plugin.inference"


def test_opentelemetry_json_export_structure():
    recorder = TraceRecorder(max_traces=10)
    trace_id = "b" * 32
    span = Span(
        name="test.span",
        trace_id=trace_id,
        span_id="c" * 16,
        service_name="sensoryplex-test",
        attributes={"http.status_code": 200, "is_retry": False, "duration": 12.5},
    )
    span.end()
    recorder.record_span(span)

    otel = recorder.export_otel_json(trace_id)
    assert otel is not None
    assert "resourceSpans" in otel
    rs = otel["resourceSpans"][0]
    assert rs["resource"]["attributes"][0]["value"]["stringValue"] == "sensoryplex-test"
    sp = rs["scopeSpans"][0]["spans"][0]
    assert sp["traceId"] == trace_id
    assert sp["spanId"] == "c" * 16
    assert sp["name"] == "test.span"


def test_tracer_and_trace_span_context_manager():
    tracer = get_tracer("test-service")
    with tracer.start_as_current_span("parent_action") as parent_span:
        assert parent_span.service_name == "test-service"
        parent_ctx = get_trace_context()
        assert parent_ctx is not None
        assert parent_ctx.trace_id == parent_span.trace_id
        assert parent_ctx.span_id == parent_span.span_id

        with trace_span("child_action", service_name="sub-service") as _child_span:
            child_ctx = get_trace_context()
            assert child_ctx.trace_id == parent_span.trace_id
            assert child_ctx.parent_span_id == parent_span.span_id

    # 确保退出后自动重置
    assert get_trace_context() is None
