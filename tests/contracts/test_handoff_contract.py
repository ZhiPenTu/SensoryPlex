"""跨进程数据面（handoff.proto）的契约校验。

这里只校验契约本身：控制消息不得携带字节、拒绝原因必须显式、两条对账恒等式
在字段层面表达得出来。真实交接由 `make handoff-check` 用独立进程验收。
"""

from edge_material_sdk.generated.media.v1 import handoff_pb2

# 恒等式在契约注释里逐字写明，测试按同样的措辞校验字段存在。
IDENTITY_FIELDS = (
    "retained_limit",
    "retained",
    "leased",
    "offered_total",
    "retained_total",
    "released_total",
    "expired_total",
    "retain_rejections",
    "request_rejections",
    "arena_capacity_bytes",
    "arena_used_bytes",
    "arena_peak_bytes",
    "arena_live_slabs",
    "retained_peak",
    "retained_kind_limit",
    "retained_kind_peak",
    "residency_samples",
    "residency_max_ms",
    "residency_total_ms",
)


def test_service_exposes_exactly_the_data_plane_calls():
    service = handoff_pb2.DESCRIPTOR.services_by_name["BufferHandoffService"]
    methods = {method.name for method in service.methods}
    assert methods == {"List", "Stats", "Acquire", "Release"}


def test_control_messages_carry_no_payload_bytes():
    """AGENTS.md：控制消息只传受控引用，绝不携带原始帧/音频/tensor。"""
    for name in ("RetainedBuffer", "AcquireBufferResponse", "ListRetainedResponse"):
        descriptor = handoff_pb2.DESCRIPTOR.message_types_by_name[name]
        assert not any(field.type == field.TYPE_BYTES for field in descriptor.fields), name


def test_acquire_request_defaults_are_not_a_hidden_wildcard():
    request = handoff_pb2.AcquireBufferRequest()
    assert request.buffer_id == ""
    assert (request.offset_bytes, request.length_bytes) == (0, 0)
    # 0 表示"服务端默认 TTL"，不是"永不过期"：语义由服务端显式实现并拒绝越界值。
    assert request.ttl_ms == 0


def test_rejections_are_explicit_not_implied_by_absence():
    response = handoff_pb2.AcquireBufferResponse()
    assert response.granted is False
    assert not response.segment_name
    assert not response.HasField("error")
    refused = handoff_pb2.AcquireBufferResponse(granted=False)
    refused.error.reason_code = "mapping_out_of_range"
    refused.error.retryable = False
    assert refused.error.reason_code == "mapping_out_of_range"


def test_stats_express_the_two_accounting_identities():
    stats = handoff_pb2.HandoffStats()
    for name in IDENTITY_FIELDS:
        assert hasattr(stats, name), name
        assert getattr(stats, name) == 0
    # 拒绝原因按原因码拆分，且与两个拒绝计数同源。
    stats.rejection_reasons["handoff_backlog_full"] = 3
    stats.retain_rejections = 3
    assert stats.retain_rejections + stats.request_rejections == sum(
        stats.rejection_reasons.values()
    )


def test_the_write_side_rejection_reasons_can_be_reconciled_on_their_own():
    """背压报告只用写侧的因果表；合并口径会让"丢弃原因之和"对不上 `retain_rejections`。"""
    stats = handoff_pb2.HandoffStats()
    assert dict(stats.retain_rejection_reasons) == {}
    stats.retain_rejection_reasons["handoff_backlog_full"] = 2
    stats.retain_rejection_reasons["arena_capacity_exceeded"] = 1
    stats.retain_rejections = 3
    # 写侧 + 读侧的合并表是另一条口径，读侧的原因不会出现在写侧表里。
    stats.request_rejections = 1
    stats.rejection_reasons["unknown_buffer"] = 1
    assert stats.retain_rejections == sum(stats.retain_rejection_reasons.values())
    assert stats.retain_rejections + stats.request_rejections == sum(
        stats.rejection_reasons.values()
    ) + sum(stats.retain_rejection_reasons.values())


def test_the_retained_window_is_split_by_kind():
    """保留表是所有种类共用的一张表，"按种类对半"这条上限必须单独读得出来。

    只看 `retained / retained_limit` 会把"某一类已满、另一类还进不来"读成"还有富余"：
    实时流里音频块 47 Hz、视频 keep 只有几 Hz，实测就是这样（32 条窗口里 32 条全是音频）。
    """
    stats = handoff_pb2.HandoffStats()
    assert dict(stats.retained_by_kind) == {}
    stats.retained = 6
    stats.retained_kind_limit = 4
    stats.retained_kind_peak = 4
    stats.retained_by_kind["audio_pcm"] = 4
    stats.retained_by_kind["video_frame"] = 2
    assert sum(stats.retained_by_kind.values()) == stats.retained
    assert max(stats.retained_by_kind.values()) <= stats.retained_kind_limit
    assert stats.retained_kind_peak <= stats.retained_kind_limit


def test_residency_has_no_value_without_a_sample():
    """没有样本时驻留统计保持 0，并且由 `residency_samples` 说明它，而不是编一个平均值。"""
    stats = handoff_pb2.HandoffStats()
    assert stats.residency_samples == 0
    assert (stats.residency_max_ms, stats.residency_total_ms) == (0, 0)
    stats.residency_samples = 2
    stats.residency_total_ms = 3_000
    stats.residency_max_ms = 2_000
    assert stats.residency_total_ms <= stats.residency_max_ms * stats.residency_samples


def test_retained_buffer_defaults_describe_nothing():
    entry = handoff_pb2.RetainedBuffer()
    assert entry.buffer_id == ""
    assert entry.length_bytes == 0
    assert entry.content_hash == ""
    assert entry.lease_id == ""
