"""SensoryPlex 统一 Prometheus 标准指标（Metrics）与 SRE 治理模块。

遵循高性能底层、零额外依赖与企业级 SRE 规范：
1. 纯 Python 标准库实现，零第三方依赖（无需 prometheus_client）；
2. 线程安全、高并发、低内存开销；
3. 支持 Counter（计数器）、Gauge（仪表盘）、Histogram（直方图/分位数估算）；
4. 输出标准 Prometheus 0.0.4 纯文本暴露格式
   （Content-Type: text/plain; version=0.0.4; charset=utf-8）；
5. 针对常驻非 HTTP 容器（index, relay, fuser）提供内置轻量级 Daemon HTTP 服务端；
6. 预置 P99 检索延迟、背压三队列丢弃计数、Lease 响应耗时、Relay 在飞数、检索命中率、
   内存水位、重试耗尽等全部关键 SRE 黄金指标。
"""

from __future__ import annotations

import http.server
import os
import re
import resource
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

DEFAULT_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


def _escape_label_value(val: Any) -> str:
    """Prometheus 标签值转义：反斜杠、双引号和换行符。"""
    s = str(val)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ""
    items = []
    for k in sorted(labels.keys()):
        if not _LABEL_NAME_RE.match(k):
            continue
        v = _escape_label_value(labels[k])
        items.append(f'{k}="{v}"')
    return f"{{{','.join(items)}}}" if items else ""


class Metric:
    """指标族基类。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"invalid_metric_name: {name}")
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.Lock()

    def _validate_labels(self, labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        labels = labels or {}
        # 允许传入多余或缺少标签，按声明的 label_names 保持一致顺序
        normalized = []
        for name in self.label_names:
            val = str(labels.get(name, ""))
            normalized.append((name, val))
        for k in sorted(labels.keys()):
            if k not in self.label_names and _LABEL_NAME_RE.match(k):
                normalized.append((k, str(labels[k])))
        return tuple(normalized)


class Counter(Metric):
    """单调递增计数器。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        if amount < 0:
            raise ValueError("counter_increment_must_be_non_negative")
        key = self._validate_labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, labels: dict[str, Any] | None = None) -> float:
        key = self._validate_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def format_lines(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            for key, val in sorted(self._values.items()):
                label_dict = dict(key)
                formatted_val = int(val) if val.is_integer() else val
                lines.append(f"{self.name}{_format_labels(label_dict)} {formatted_val}")
        return lines


class Gauge(Metric):
    """可增可减即时仪表盘。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, labels: dict[str, Any] | None = None) -> None:
        key = self._validate_labels(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = self._validate_labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = self._validate_labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def get(self, labels: dict[str, Any] | None = None) -> float:
        key = self._validate_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def format_lines(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            for key, val in sorted(self._values.items()):
                label_dict = dict(key)
                formatted_val = int(val) if val.is_integer() else val
                lines.append(f"{self.name}{_format_labels(label_dict)} {formatted_val}")
        return lines


class Histogram(Metric):
    """直方图，按分桶累计采样，统计 P50/P90/P99 延迟与累计和。"""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ):
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(sorted(buckets))
        self._data: dict[
            tuple[tuple[str, str], ...],
            dict[str, Any],  # {"buckets": {b: count}, "sum": float, "count": int}
        ] = {}

    def observe(self, value: float, labels: dict[str, Any] | None = None) -> None:
        key = self._validate_labels(labels)
        val = float(value)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                entry = {
                    "buckets": {b: 0 for b in self.buckets},
                    "sum": 0.0,
                    "count": 0,
                }
                self._data[key] = entry

            entry["count"] += 1
            entry["sum"] += val
            for b in self.buckets:
                if val <= b:
                    entry["buckets"][b] += 1

    @contextmanager
    def time(self, labels: dict[str, Any] | None = None) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(time.perf_counter() - start, labels=labels)

    def format_lines(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with self._lock:
            for key, entry in sorted(self._data.items()):
                label_dict = dict(key)
                # 依次打印 _bucket 行
                for b in self.buckets:
                    b_labels = dict(label_dict)
                    b_labels["le"] = str(b)
                    lines.append(
                        f"{self.name}_bucket{_format_labels(b_labels)} {entry['buckets'][b]}"
                    )
                inf_labels = dict(label_dict)
                inf_labels["le"] = "+Inf"
                lines.append(f"{self.name}_bucket{_format_labels(inf_labels)} {entry['count']}")

                # sum 与 count 行
                sum_val = round(entry["sum"], 6)
                lines.append(f"{self.name}_sum{_format_labels(label_dict)} {sum_val}")
                lines.append(f"{self.name}_count{_format_labels(label_dict)} {entry['count']}")
        return lines


class MetricsRegistry:
    """指标注册表。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._metrics: dict[str, Metric] = {}

    def register_counter(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> Counter:
        with self._lock:
            if name in self._metrics:
                m = self._metrics[name]
                if isinstance(m, Counter):
                    return m
                raise ValueError(f"metric_type_conflict: {name}")
            c = Counter(name, help_text, label_names)
            self._metrics[name] = c
            return c

    def register_gauge(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Gauge:
        with self._lock:
            if name in self._metrics:
                m = self._metrics[name]
                if isinstance(m, Gauge):
                    return m
                raise ValueError(f"metric_type_conflict: {name}")
            g = Gauge(name, help_text, label_names)
            self._metrics[name] = g
            return g

    def register_histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ) -> Histogram:
        with self._lock:
            if name in self._metrics:
                m = self._metrics[name]
                if isinstance(m, Histogram):
                    return m
                raise ValueError(f"metric_type_conflict: {name}")
            h = Histogram(name, help_text, label_names, buckets=buckets)
            self._metrics[name] = h
            return h

    def get_metric(self, name: str) -> Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def format_prometheus(self) -> str:
        """导出符合 Prometheus 规范的 exposition 纯文本。"""
        lines: list[str] = []
        with self._lock:
            for _, metric in sorted(self._metrics.items()):
                m_lines = metric.format_lines()
                if m_lines:
                    lines.extend(m_lines)
        return "\n".join(lines) + ("\n" if lines else "")


# ── 全局单例与预置 SRE 关键黄金指标 ─────────────────────────────────────────

_GLOBAL_REGISTRY: MetricsRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> MetricsRegistry:
    """获取全局共享 MetricsRegistry，并预先注册标准 SRE 指标。"""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _REGISTRY_LOCK:
            if _GLOBAL_REGISTRY is None:
                r = MetricsRegistry()
                _init_standard_sre_metrics(r)
                _GLOBAL_REGISTRY = r
    return _GLOBAL_REGISTRY


def _init_standard_sre_metrics(r: MetricsRegistry) -> None:
    # 1. 检索耗时直方图（用于计算 P99/P90/P50 检索延迟）
    r.register_histogram(
        "sensoryplex_search_duration_seconds",
        "Material retrieval search duration in seconds",
        label_names=("mode", "status"),
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    )
    # 2. 检索命中与查询总数（用于计算检索命中率）
    r.register_counter(
        "sensoryplex_search_queries_total",
        "Total number of material search queries",
        label_names=("mode", "has_hits"),
    )
    r.register_counter(
        "sensoryplex_search_hits_total",
        "Total number of materials hit by search queries",
        label_names=("mode",),
    )
    # 3. 背压三队列丢弃计数（低/中/高三档丢弃）
    r.register_counter(
        "sensoryplex_backpressure_dropped_total",
        "Total dropped items due to multi-tier backpressure capacity limits",
        label_names=("tier", "queue", "reason"),
    )
    r.register_gauge(
        "sensoryplex_queue_depth",
        "Current item depth of backpressure queues",
        label_names=("queue",),
    )
    r.register_gauge(
        "sensoryplex_queue_capacity",
        "Max allowed capacity of backpressure queues",
        label_names=("queue",),
    )
    # 4. Lease 响应耗时
    r.register_histogram(
        "sensoryplex_lease_response_duration_seconds",
        "Duration of buffer lease acquire/release operations in seconds",
        label_names=("operation", "status"),
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0),
    )
    # 5. Relay 在飞数与发布统计
    r.register_gauge(
        "sensoryplex_relay_inflight",
        "Current inflight uncommitted events in outbox relay",
        label_names=("stream", "subject"),
    )
    r.register_counter(
        "sensoryplex_relay_events_published_total",
        "Total events successfully published to JetStream by relay",
        label_names=("stream", "subject"),
    )
    r.register_counter(
        "sensoryplex_relay_events_failed_total",
        "Total event publishing failures in outbox relay",
        label_names=("stream", "subject", "reason"),
    )
    # 6. 内存水位
    r.register_gauge(
        "sensoryplex_memory_bytes",
        "Process and container memory usage in bytes",
        label_names=("service", "kind"),
    )
    # 7. 重试耗尽计数
    r.register_counter(
        "sensoryplex_retry_exhausted_total",
        "Total operations that exhausted all max retry attempts",
        label_names=("component", "reason"),
    )
    # 8. HTTP 业务接口耗时
    r.register_histogram(
        "sensoryplex_http_request_duration_seconds",
        "HTTP request latency in seconds",
        label_names=("service", "method", "path", "status"),
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    )
    # 9. Fuser 结果融合统计
    r.register_counter(
        "sensoryplex_fuser_results_total",
        "Total slowpath results processed by fuser",
        label_names=("modality", "status"),
    )
    r.register_counter(
        "sensoryplex_fuser_retries_total",
        "Total retries triggered during result fusing",
        label_names=("modality", "reason"),
    )


# ── 进程内存水位探测 ─────────────────────────────────────────────────────────


def update_process_memory_metrics(service_name: str) -> None:
    """采集当前进程常驻内存（RSS）并更新 sensoryplex_memory_bytes 仪表。"""
    rss_bytes = 0
    # 1. 优先读取 Linux /proc/self/statm (页数 * 4096)
    if os.path.exists("/proc/self/statm"):
        try:
            with open("/proc/self/statm") as f:
                parts = f.read().split()
                if len(parts) >= 2:
                    rss_bytes = int(parts[1]) * 4096
        except Exception:
            pass

    # 2. 回退使用 getrusage (macOS 为 bytes，Linux 为 KiB)
    if rss_bytes <= 0:
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            maxrss = usage.ru_maxrss
            # Linux: KiB, macOS/BSD: bytes
            if os.uname().sysname == "Darwin":
                rss_bytes = maxrss
            else:
                rss_bytes = maxrss * 1024
        except Exception:
            pass

    if rss_bytes > 0:
        reg = get_registry()
        g = reg.get_metric("sensoryplex_memory_bytes")
        if isinstance(g, Gauge):
            g.set(rss_bytes, labels={"service": service_name, "kind": "resident"})


# ── 轻量级 Daemon HTTP 指标服务器（供 index / relay / fuser 等进程暴露） ────


class MetricsServer:
    """常驻后台的 HTTP /metrics 服务端。"""

    def __init__(
        self,
        port: int,
        registry: MetricsRegistry | None = None,
        host: str = "0.0.0.0",
        service_name: str = "sensoryplex",
    ):
        self.port = port
        self.host = host
        self.service_name = service_name
        self.registry = registry or get_registry()
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        reg = self.registry
        service_name = self.service_name

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                # 静默健康检查与抓取日志，避免刷屏
                pass

            def do_GET(self) -> None:
                if self.path in ("/metrics", "/metrics/"):
                    update_process_memory_metrics(service_name)
                    body = reg.format_prometheus().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path in ("/healthz", "/livez"):
                    body = b'{"status":"ok"}\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        # 允许端口快速重用
        class _ReusableServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = True

        self._server = _ReusableServer((self.host, self.port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"metrics-server-{self.service_name}-{self.port}",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def start_metrics_server(
    port: int,
    registry: MetricsRegistry | None = None,
    host: str = "0.0.0.0",
    service_name: str = "sensoryplex",
) -> MetricsServer:
    """启动轻量级后台指标服务器。"""
    server = MetricsServer(port=port, registry=registry, host=host, service_name=service_name)
    server.start()
    return server
