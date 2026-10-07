"""Prometheus 标准指标端点与 SRE 治理契约测试。"""

import time

from edge_material_sdk.metrics import (
    Gauge,
    MetricsRegistry,
    get_registry,
    start_metrics_server,
    update_process_memory_metrics,
)


def test_counter_semantics_and_formatting():
    reg = MetricsRegistry()
    c = reg.register_counter(
        "test_requests_total", "Total requests", label_names=("method", "code")
    )
    c.inc(1, labels={"method": "GET", "code": 200})
    c.inc(2, labels={"method": "GET", "code": 200})
    c.inc(1, labels={"method": "POST", "code": 500})

    assert c.get(labels={"method": "GET", "code": 200}) == 3.0
    assert c.get(labels={"method": "POST", "code": 500}) == 1.0

    text = reg.format_prometheus()
    assert "# HELP test_requests_total Total requests\n" in text
    assert "# TYPE test_requests_total counter\n" in text
    assert 'test_requests_total{code="200",method="GET"} 3' in text
    assert 'test_requests_total{code="500",method="POST"} 1' in text


def test_gauge_set_inc_dec():
    reg = MetricsRegistry()
    g = reg.register_gauge("test_queue_depth", "Queue depth", label_names=("queue",))
    g.set(10, labels={"queue": "events"})
    assert g.get(labels={"queue": "events"}) == 10.0

    g.inc(5, labels={"queue": "events"})
    assert g.get(labels={"queue": "events"}) == 15.0

    g.dec(3, labels={"queue": "events"})
    assert g.get(labels={"queue": "events"}) == 12.0

    text = reg.format_prometheus()
    assert "# TYPE test_queue_depth gauge\n" in text
    assert 'test_queue_depth{queue="events"} 12' in text


def test_histogram_buckets_and_time_context_manager():
    reg = MetricsRegistry()
    h = reg.register_histogram(
        "test_latency_seconds",
        "Latency in seconds",
        label_names=("route",),
        buckets=(0.01, 0.05, 0.1, 0.5),
    )

    h.observe(0.005, labels={"route": "search"})
    h.observe(0.02, labels={"route": "search"})
    h.observe(0.08, labels={"route": "search"})

    with h.time(labels={"route": "search"}):
        time.sleep(0.001)

    text = reg.format_prometheus()
    assert "# TYPE test_latency_seconds histogram\n" in text
    assert 'test_latency_seconds_bucket{le="0.01",route="search"}' in text
    assert 'test_latency_seconds_bucket{le="+Inf",route="search"} 4' in text
    assert 'test_latency_seconds_count{route="search"} 4' in text
    assert 'test_latency_seconds_sum{route="search"}' in text


def test_standard_sre_metrics_pre_registered():
    reg = get_registry()
    # 验证全部关键 SRE 黄金指标均已就绪
    expected = [
        "sensoryplex_search_duration_seconds",
        "sensoryplex_search_queries_total",
        "sensoryplex_search_hits_total",
        "sensoryplex_backpressure_dropped_total",
        "sensoryplex_queue_depth",
        "sensoryplex_queue_capacity",
        "sensoryplex_lease_response_duration_seconds",
        "sensoryplex_relay_inflight",
        "sensoryplex_relay_events_published_total",
        "sensoryplex_relay_events_failed_total",
        "sensoryplex_memory_bytes",
        "sensoryplex_retry_exhausted_total",
        "sensoryplex_http_request_duration_seconds",
        "sensoryplex_fuser_results_total",
        "sensoryplex_fuser_retries_total",
    ]
    for name in expected:
        assert reg.get_metric(name) is not None, f"Missing SRE metric: {name}"


def test_process_memory_metrics_sampling():
    update_process_memory_metrics("test-worker")
    reg = get_registry()
    mem_metric = reg.get_metric("sensoryplex_memory_bytes")
    assert isinstance(mem_metric, Gauge)
    val = mem_metric.get(labels={"service": "test-worker", "kind": "resident"})
    # 真实进程常驻内存必须大于 0
    assert val > 0


def test_metrics_http_server_endpoints():
    import urllib.request

    reg = MetricsRegistry()
    g = reg.register_gauge("sample_metric", "Sample metric")
    g.set(42)

    server = start_metrics_server(
        port=59123, registry=reg, host="127.0.0.1", service_name="test-daemon"
    )
    try:
        # 1. 抓取 /metrics
        req = urllib.request.urlopen("http://127.0.0.1:59123/metrics", timeout=2)
        assert req.status == 200
        content_type = req.headers.get("Content-Type")
        assert "text/plain" in content_type
        assert "version=0.0.4" in content_type
        body = req.read().decode("utf-8")
        assert "sample_metric 42" in body

        # 2. 探活 /healthz
        req_health = urllib.request.urlopen("http://127.0.0.1:59123/healthz", timeout=2)
        assert req_health.status == 200
    finally:
        server.stop()
