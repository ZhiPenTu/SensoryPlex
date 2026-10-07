"""SensoryPlex OpenTelemetry 分布式链路追踪与 W3C TraceContext 规范实现。

按照高性能底层与金融级应用规范设计：
1. 完整实现 W3C TraceContext 规范（traceparent: version-trace_id-parent_id-trace_flags）；
2. 跨进程全链路透传：支持 HTTP Headers、NATS Message Headers、gRPC Metadata 与数据库任务关联表；
3. 轻量化零额外依赖：基于 Python 标准库构建，与所有微服务与插件兼容；
4. 内存有界性：内置高并发线程安全有界环形缓冲（RingBuffer）TraceRecorder；
5. 端到端耗时瀑布图（Waterfall）与瓶颈定位分析（Bottleneck Detection）；
6. 兼容易读的 OpenTelemetry JSON 格式导出。
"""

from __future__ import annotations

import collections
import contextvars
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .logger import sanitize_dict, set_trace_id

# W3C TraceContext 正则校验
# version: 00
# trace_id: 32 hex chars (16 bytes, 不能全 0)
# span_id/parent_id: 16 hex chars (8 bytes, 不能全 0)
# trace_flags: 2 hex chars
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$", re.IGNORECASE
)
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16


@dataclass(frozen=True)
class TraceContext:
    """不可变 W3C TraceContext 载体。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    trace_flags: str = "01"
    tracestate: str = ""

    @property
    def traceparent(self) -> str:
        """格式化为标准 W3C traceparent 头部字符串。"""
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @property
    def is_sampled(self) -> bool:
        """是否采样标志位。"""
        try:
            return bool(int(self.trace_flags, 16) & 0x01)
        except ValueError:
            return True

    def create_child(self) -> TraceContext:
        """派生下游子 Span 对应的 TraceContext。"""
        child_span_id = secrets.token_hex(8)
        return TraceContext(
            trace_id=self.trace_id,
            span_id=child_span_id,
            parent_span_id=self.span_id,
            trace_flags=self.trace_flags,
            tracestate=self.tracestate,
        )

    @classmethod
    def new_root(cls, sampled: bool = True, tracestate: str = "") -> TraceContext:
        """生成新的根 TraceContext。"""
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        flags = "01" if sampled else "00"
        return cls(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            trace_flags=flags,
            tracestate=tracestate,
        )

    @classmethod
    def from_traceparent(
        cls,
        header: str | None,
        tracestate: str = "",
        *,
        generate_child_span: bool = True,
    ) -> TraceContext | None:
        """解析 W3C traceparent 头部。不合法时显式返回 None。"""
        if not header:
            return None
        header = header.strip()
        match = _TRACEPARENT_RE.match(header)
        if not match:
            return None
        version, trace_id, incoming_span_id, flags = match.groups()
        # 目前只支持 version 00
        if version != "00":
            return None
        trace_id = trace_id.lower()
        incoming_span_id = incoming_span_id.lower()
        flags = flags.lower()
        if trace_id == _ZERO_TRACE_ID or incoming_span_id == _ZERO_SPAN_ID:
            return None

        if generate_child_span:
            child_span_id = secrets.token_hex(8)
            return cls(
                trace_id=trace_id,
                span_id=child_span_id,
                parent_span_id=incoming_span_id,
                trace_flags=flags,
                tracestate=tracestate,
            )
        return cls(
            trace_id=trace_id,
            span_id=incoming_span_id,
            parent_span_id=None,
            trace_flags=flags,
            tracestate=tracestate,
        )


# ── 上下文变量与全局状态 ─────────────────────────────────────────────────────

_CURRENT_TRACE_CONTEXT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "sensoryplex_current_trace_context", default=None
)


def get_trace_context() -> TraceContext | None:
    """获取当前协程或线程绑定的 TraceContext。"""
    return _CURRENT_TRACE_CONTEXT.get()


def set_trace_context(ctx: TraceContext | None) -> None:
    """绑定当前协程或线程的 TraceContext，同时自动同步 logger 的 trace_id。"""
    _CURRENT_TRACE_CONTEXT.set(ctx)
    set_trace_id(ctx.trace_id if ctx else None)


def get_or_create_trace_context() -> TraceContext:
    """获取当前上下文，若无则生成新的根上下文。"""
    ctx = _CURRENT_TRACE_CONTEXT.get()
    if ctx is None:
        ctx = TraceContext.new_root()
        set_trace_context(ctx)
    return ctx


@contextmanager
def trace_context_scope(ctx: TraceContext) -> Iterator[TraceContext]:
    """上下文管理器：在作用域内临时绑定 TraceContext。"""
    token = _CURRENT_TRACE_CONTEXT.set(ctx)
    set_trace_id(ctx.trace_id)
    try:
        yield ctx
    finally:
        _CURRENT_TRACE_CONTEXT.reset(token)
        prev = _CURRENT_TRACE_CONTEXT.get()
        set_trace_id(prev.trace_id if prev else None)


# ── Span 数据结构与有界追踪记录器 ───────────────────────────────────────────


@dataclass
class Span:
    """单段链路追踪跨度（Span）。"""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service_name: str = "sensoryplex"
    start_time_ns: int = field(default_factory=time.time_ns)
    end_time_ns: int | None = None
    status: str = "UNSET"  # "OK", "ERROR", "UNSET"
    status_message: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        """Span 耗时（毫秒）。未结束时计算至当前时刻。"""
        end = self.end_time_ns or time.time_ns()
        return round((end - self.start_time_ns) / 1_000_000, 3)

    def set_attribute(self, key: str, value: Any) -> Span:
        """安全设置业务属性（自动过滤敏感字段）。"""
        self.attributes[key] = value
        return self

    def set_attributes(self, attrs: dict[str, Any]) -> Span:
        """批量设置业务属性。"""
        self.attributes.update(attrs)
        return self

    def set_status(self, status: str, message: str = "") -> Span:
        """设置状态结果。"""
        self.status = status.upper()
        self.status_message = message
        return self

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """添加时间点事件。"""
        self.events.append(
            {
                "name": name,
                "timestamp_ns": time.time_ns(),
                "attributes": sanitize_dict(attributes or {}),
            }
        )
        return self

    def end(self, end_time_ns: int | None = None) -> None:
        """结束 Span 并记录结束时间戳。"""
        if self.end_time_ns is None:
            self.end_time_ns = end_time_ns or time.time_ns()
            if self.status == "UNSET":
                self.status = "OK"
            get_trace_recorder().record_span(self)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 结构。"""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "service_name": self.service_name,
            "start_time_unix_ms": self.start_time_ns // 1_000_000,
            "end_time_unix_ms": (self.end_time_ns // 1_000_000) if self.end_time_ns else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "status_message": self.status_message,
            "attributes": sanitize_dict(self.attributes),
            "events": self.events,
        }

    def to_otel_dict(self) -> dict[str, Any]:
        """导出为符合 OpenTelemetry 标准协议规范的 Span 对象。"""
        otel_attributes = []
        for k, v in sanitize_dict(self.attributes).items():
            val_dict: dict[str, Any] = {}
            if isinstance(v, bool):
                val_dict = {"boolValue": v}
            elif isinstance(v, int):
                val_dict = {"intValue": v}
            elif isinstance(v, float):
                val_dict = {"doubleValue": v}
            else:
                val_dict = {"stringValue": str(v)}
            otel_attributes.append({"key": k, "value": val_dict})

        status_code = {"OK": 1, "ERROR": 2}.get(self.status, 0)
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "kind": 1,  # SPAN_KIND_INTERNAL / SERVER
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns or time.time_ns()),
            "attributes": otel_attributes,
            "status": {"code": status_code, "message": self.status_message},
        }


class TraceRecorder:
    """线程安全、高并发、有界内存的 Trace 存储记录器。"""

    def __init__(self, max_traces: int = 1000):
        self._max_traces = max(10, min(max_traces, 10000))
        self._lock = threading.Lock()
        # 保持 trace_id 插入顺序的 LRU 字典
        self._traces: collections.OrderedDict[str, list[Span]] = collections.OrderedDict()

    def record_span(self, span: Span) -> None:
        with self._lock:
            spans = self._traces.setdefault(span.trace_id, [])
            spans.append(span)
            self._traces.move_to_end(span.trace_id)
            while len(self._traces) > self._max_traces:
                self._traces.popitem(last=False)

    def get_trace_spans(self, trace_id: str) -> list[Span]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def get_waterfall(self, trace_id: str) -> dict[str, Any] | None:
        """构建端到端瀑布图与瓶颈定位分析。"""
        spans = self.get_trace_spans(trace_id)
        if not spans:
            return None

        sorted_spans = sorted(spans, key=lambda s: s.start_time_ns)
        min_start = sorted_spans[0].start_time_ns
        max_end = max((s.end_time_ns or s.start_time_ns) for s in sorted_spans)
        total_duration_ms = max(0.001, round((max_end - min_start) / 1_000_000, 3))

        # 构建 Span 映射与父子关系
        span_map: dict[str, dict[str, Any]] = {}
        children_map: dict[str | None, list[dict[str, Any]]] = collections.defaultdict(list)

        bottleneck_span = None
        max_span_duration = -1.0

        for s in sorted_spans:
            d_ms = s.duration_ms
            if d_ms > max_span_duration:
                max_span_duration = d_ms
                bottleneck_span = s

            offset_ms = round((s.start_time_ns - min_start) / 1_000_000, 3)
            node = {
                **s.to_dict(),
                "offset_ms": offset_ms,
                "percentage_of_total": round((d_ms / total_duration_ms) * 100, 1),
                "children": [],
            }
            span_map[s.span_id] = node
            children_map[s.parent_span_id].append(node)

        # 关联树形结构
        roots: list[dict[str, Any]] = []
        for s in sorted_spans:
            node = span_map[s.span_id]
            node["children"] = children_map.get(s.span_id, [])
            if s.parent_span_id is None or s.parent_span_id not in span_map:
                roots.append(node)

        bottleneck_info = None
        if bottleneck_span:
            bottleneck_info = {
                "span_id": bottleneck_span.span_id,
                "name": bottleneck_span.name,
                "service_name": bottleneck_span.service_name,
                "duration_ms": bottleneck_span.duration_ms,
                "percentage": round((bottleneck_span.duration_ms / total_duration_ms) * 100, 1),
            }

        return {
            "trace_id": trace_id,
            "total_duration_ms": total_duration_ms,
            "span_count": len(sorted_spans),
            "bottleneck": bottleneck_info,
            "waterfall": roots,
            "spans": [span_map[s.span_id] for s in sorted_spans],
        }

    def export_otel_json(self, trace_id: str) -> dict[str, Any] | None:
        """导出为 OpenTelemetry ResourceSpans 标准结构。"""
        spans = self.get_trace_spans(trace_id)
        if not spans:
            return None

        # 按 service_name 聚合
        by_service: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for s in spans:
            by_service[s.service_name].append(s.to_otel_dict())

        resource_spans = []
        for service, otel_spans in by_service.items():
            resource_spans.append(
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "sensoryplex-telemetry", "version": "0.1.0"},
                            "spans": otel_spans,
                        }
                    ],
                }
            )
        return {"resourceSpans": resource_spans}


_RECORDER: TraceRecorder | None = None
_RECORDER_LOCK = threading.Lock()


def get_trace_recorder() -> TraceRecorder:
    """获取全局单例 TraceRecorder。"""
    global _RECORDER
    if _RECORDER is None:
        with _RECORDER_LOCK:
            if _RECORDER is None:
                _RECORDER = TraceRecorder()
    return _RECORDER


class Tracer:
    """应用级 Tracer 实例。"""

    def __init__(self, service_name: str = "sensoryplex"):
        self.service_name = service_name

    def start_span(
        self,
        name: str,
        parent: TraceContext | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """创建新 Span。"""
        current_ctx = parent or get_trace_context()
        if current_ctx is None:
            ctx = TraceContext.new_root()
            parent_id = None
        else:
            ctx = current_ctx.create_child()
            parent_id = current_ctx.span_id

        span = Span(
            name=name,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=parent_id,
            service_name=self.service_name,
            attributes=dict(attributes or {}),
        )
        return span

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        parent: TraceContext | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """上下文管理器：启动一个 Span 并作为当前上下文。"""
        span = self.start_span(name, parent=parent, attributes=attributes)
        new_ctx = TraceContext(
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
        )
        token = _CURRENT_TRACE_CONTEXT.set(new_ctx)
        set_trace_id(span.trace_id)
        try:
            yield span
        except Exception as exc:
            span.set_status("ERROR", str(exc))
            span.add_event(
                "exception",
                {"exception.type": type(exc).__name__, "exception.message": str(exc)},
            )
            raise
        finally:
            span.end()
            _CURRENT_TRACE_CONTEXT.reset(token)
            prev = _CURRENT_TRACE_CONTEXT.get()
            set_trace_id(prev.trace_id if prev else None)


def get_tracer(service_name: str = "sensoryplex") -> Tracer:
    """创建或获取指定服务名的 Tracer。"""
    return Tracer(service_name=service_name)


@contextmanager
def trace_span(
    name: str,
    service_name: str = "sensoryplex",
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """便捷上下文管理器：快速包裹一个代码块进行追踪。"""
    tracer = get_tracer(service_name)
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span
