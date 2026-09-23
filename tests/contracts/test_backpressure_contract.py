"""背压可观察量（media.proto 的 BackpressureReport）的契约校验。

这里只校验契约本身：一条"没有队列可测"的运行必须和"测到零压力"区分得开，
丢弃必须带原因，水位必须同时给出容量与峰值。真实的非零指标由
`make media-replay` / `make live-check` 在真实样本上产出，不在这里断言。
"""

from edge_material_sdk.generated.media.v1 import media_pb2

IDENTITY_FIELDS = (
    "degraded_entries",
    "saturated_entries",
    "dropped_total",
    "timeouts_total",
    "residency_samples",
    "residency_max_ms",
    "residency_avg_ms",
    "sampling_throttled_samples",
    "throttle_factor",
)


def test_a_default_report_never_reads_as_zero_pressure():
    report = media_pb2.ReplayReport().decoded.backpressure
    assert report.observed is False
    assert report.state == ""
    assert list(report.queues) == []
    assert report.dropped_total == 0
    assert report.sampling_throttled_samples == 0


def test_the_three_state_names_are_the_contract():
    assert {"ok", "degraded", "saturated"} == {"ok", "degraded", "saturated"}
    report = media_pb2.BackpressureReport(observed=True, state="saturated")
    assert report.state == "saturated"
    assert report.observed is True


def test_queue_water_marks_carry_a_unit_capacity_and_peak():
    report = media_pb2.BackpressureReport(observed=True)
    table = report.queues.add()
    table.name = "handoff_retained_table"
    table.unit = "items"
    table.capacity = 8
    table.current = 8
    table.peak = 8
    arena = report.queues.add()
    arena.name = "handoff_arena_bytes"
    arena.unit = "bytes"
    arena.capacity = 1_048_576
    arena.current = 65_536
    arena.peak = 1_048_576
    for queue in report.queues:
        assert queue.unit in {"items", "bytes"}
        assert queue.capacity > 0
        assert queue.peak >= queue.current, queue.name


def test_every_drop_is_explained_and_timeouts_are_counted_separately():
    report = media_pb2.BackpressureReport(observed=True, dropped_total=3)
    report.drop_reasons.add(reason="handoff_backlog_full", count=2)
    report.drop_reasons.add(reason="arena_capacity_exceeded", count=1)
    assert report.dropped_total == sum(entry.count for entry in report.drop_reasons)
    # 超时是"消费者没在 TTL 内释放"，与丢弃不是同一件事，必须分开计数。
    report.timeouts_total = 1
    assert report.timeouts_total != report.dropped_total


def test_the_total_can_be_read_by_buffer_kind():
    """保留表是所有 buffer 种类共用的，只给总数会被读成"视频帧全丢了"。"""
    report = media_pb2.BackpressureReport(observed=True, dropped_total=3)
    report.drop_kinds.add(kind="video_frame", count=2)
    report.drop_kinds.add(kind="audio_pcm", count=1)
    assert report.dropped_total == sum(entry.count for entry in report.drop_kinds)
    assert {entry.kind for entry in report.drop_kinds} == {"video_frame", "audio_pcm"}
    assert media_pb2.BackpressureReport().drop_kinds == []


def test_no_raw_bytes_can_hide_in_the_report():
    """AGENTS.md：控制消息只传受控引用，绝不携带原始帧/音频/tensor。"""
    descriptor = media_pb2.DESCRIPTOR.message_types_by_name["BackpressureReport"]
    for name in ("BackpressureQueue", "BackpressureDropReason"):
        nested = media_pb2.DESCRIPTOR.message_types_by_name[name]
        assert not any(field.type == field.TYPE_BYTES for field in nested.fields), name
    assert not any(field.type == field.TYPE_BYTES for field in descriptor.fields), (
        "BackpressureReport"
    )


def test_the_counters_exist_on_the_message_and_start_at_zero():
    report = media_pb2.BackpressureReport()
    for name in IDENTITY_FIELDS:
        assert hasattr(report, name), name
        assert getattr(report, name) == 0
    # 采样侧的同一件事：被背压抑制的 keep 有自己的计数器与原因。
    sampling = media_pb2.SamplingReport()
    assert sampling.skipped_backpressure_throttled == 0
    assert media_pb2.DecodedDataPlane().HasField("backpressure") is False
