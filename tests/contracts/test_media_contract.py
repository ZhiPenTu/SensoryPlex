"""media source 与 replay report 消息的契约校验。"""

from edge_material_sdk.generated.common.v1 import common_pb2
from edge_material_sdk.generated.media.v1 import media_pb2


def test_source_kinds_are_explicit():
    assert media_pb2.MediaSourceKind.Value("MEDIA_SOURCE_KIND_UNSPECIFIED") == 0
    assert media_pb2.MediaSourceKind.Value("MEDIA_SOURCE_KIND_FILE") == 1
    assert media_pb2.MediaSourceKind.Value("MEDIA_SOURCE_KIND_SRT") == 2


def test_replay_report_defaults_claim_nothing():
    report = media_pb2.ReplayReport()
    assert report.golden_path_verified is False
    assert report.emitted_anchors == 0
    assert report.decoded_items == 0
    assert list(report.blockers) == []
    assert list(report.drop_reasons) == []


def test_anchor_round_trips_as_a_half_open_range():
    anchor = media_pb2.TimelineAnchor(
        anchor_id="video-00000001",
        track_kind="video",
        pts_ms=40,
        keyframe=True,
    )
    anchor.time_range.start_ms = 40
    anchor.time_range.end_ms = 80
    assert anchor.time_range.end_ms > anchor.time_range.start_ms
    assert anchor.pts_ms == anchor.time_range.start_ms


def test_decoded_plane_defaults_claim_nothing():
    """全部为 0 表示"没有执行解码"，因此不能把该构建判为成功。"""
    decoded = media_pb2.ReplayReport().decoded
    assert decoded.arena_id == ""
    assert decoded.arena_capacity_bytes == 0
    assert decoded.decoded_bytes == 0
    assert list(decoded.tracks) == []
    assert decoded.descriptors_built == 0
    assert decoded.descriptors_validated == 0
    assert decoded.descriptor_failures == 0
    assert list(decoded.failure_reasons) == []
    assert decoded.leases_issued == 0
    assert decoded.leases_released == 0
    assert list(decoded.evidence_descriptors) == []


def test_track_stat_reports_unknown_timestamps_as_minus_one():
    stat = media_pb2.DecodedTrackStat(
        track_kind="video", samples=3, first_pts_ms=-1, last_end_ms=-1
    )
    assert stat.first_pts_ms == -1
    assert stat.last_end_ms == -1
    assert stat.overlapping_samples == 0
    assert stat.timeline_offset_ms == 0
    assert list(stat.drop_reasons) == []
    empty = media_pb2.DecodedTrackStat()
    assert empty.track_kind == ""
    assert empty.samples == 0


def test_audio_segment_report_defaults_are_empty():
    segments = media_pb2.AudioSegmentReport()
    assert segments.segment_ms == 0
    assert segments.segments == 0
    assert segments.partial_segments == 0
    assert segments.bytes == 0
    assert list(segments.listed) == []
    assert list(segments.drop_reasons) == []


def test_sampling_report_defaults_claim_nothing_was_sampled():
    """空 sampling 只表示"这次没有抽帧执行"，不能读作"所有帧都被保留"。"""
    decoded = media_pb2.ReplayReport().decoded
    assert list(decoded.sampling) == []
    sampling = media_pb2.SamplingReport()
    assert sampling.track_kind == ""
    assert sampling.observed == 0
    assert sampling.kept == 0
    assert sampling.max_keeps_bound == 0
    assert sampling.max_gap_ms == 0
    assert sampling.min_interval_ms == 0 and sampling.static_hold_ms == 0
    assert sampling.change_threshold == 0


def test_sampling_report_accounts_for_every_observed_frame():
    """每个观测到的帧要么 keep、要么带原因 skip，没有第三种归宿。"""
    sampling = media_pb2.SamplingReport(
        observed=10, kept=4, skipped_rate_limited=3, skipped_no_change=3
    )
    skipped = (
        sampling.skipped_rate_limited
        + sampling.skipped_no_change
        + sampling.skipped_non_monotonic
        + sampling.skipped_missing_signature
    )
    assert sampling.observed == sampling.kept + skipped


def test_decoded_plane_round_trips_descriptor_evidence():
    report = media_pb2.ReplayReport()
    decoded = report.decoded
    decoded.arena_id = "arena-0123456789ab"
    decoded.arena_capacity_bytes = 67_108_864
    decoded.arena_peak_bytes = 2_073_600
    decoded.decoded_bytes = 1_908_975_616
    decoded.descriptors_built = 2246
    decoded.descriptors_validated = 2246
    decoded.leases_issued = 2246
    decoded.leases_released = 2246
    stat = decoded.tracks.add()
    stat.track_kind = "video"
    stat.samples = 918
    stat.timeline_offset_ms = 167
    decoded.audio_segments.segment_ms = 5000
    decoded.audio_segments.segments = 7
    decoded.audio_segments.partial_segments = 1
    segment = decoded.audio_segments.listed.add()
    segment.segment_id = "seg-0123456789ab-00000001"
    segment.time_range.start_ms = 0
    segment.time_range.end_ms = 5015
    segment.bytes = 884_736
    descriptor = decoded.evidence_descriptors.add()
    descriptor.buffer_id = "buf-0123456789ab-video_frame-00000001"
    descriptor.kind = "video_frame"
    descriptor.memory_kind = "cpu_shared_memory"
    descriptor.stream_id = "stream-0123456789ab"
    descriptor.locator.handle = decoded.arena_id
    descriptor.locator.length = 2_073_600
    descriptor.time_range.start_ms = 0
    descriptor.time_range.end_ms = 33
    descriptor.lease.lease_id = "lease-0000000000000001"
    descriptor.lease.expires_at_unix_ms = 1_790_088_324_421
    descriptor.lease.read_only = True
    descriptor.content_hash = "sha256:" + "0" * 64

    parsed = media_pb2.ReplayReport.FromString(report.SerializeToString())
    assert parsed.decoded.arena_id == decoded.arena_id
    assert parsed.decoded.descriptors_validated == parsed.decoded.descriptors_built
    assert parsed.decoded.tracks[0].timeline_offset_ms == 167
    assert parsed.decoded.audio_segments.listed[0].time_range.end_ms == 5015
    evidence = parsed.decoded.evidence_descriptors[0]
    assert evidence.locator.handle == parsed.decoded.arena_id
    assert evidence.lease.read_only is True
    assert not parsed.golden_path_verified


def test_audio_sample_layout_is_explicit_and_defaults_to_unknown():
    """音频样本布局是跨进程契约的一部分，缺省为空只表示"未知"。

    段描述符要进数据面、插件的输入要按它解释字节，所以这里既不能有默认布局，
    也不能靠"4 字节/样本"这种猜测把未知布局读成已知布局。
    """
    assert common_pb2.BufferFormat().sample_format == ""
    assert media_pb2.AudioSegment().sample_format == ""
    assert media_pb2.DecodedTrackStat().audio_format == ""

    descriptor = common_pb2.BufferDescriptor(
        buffer_id="seg-0123456789ab-00000001",
        kind="audio_segment",
        memory_kind="cpu_shared_memory",
        stream_id="stream-0123456789ab",
        format=common_pb2.BufferFormat(sample_rate=48_000, channels=2, sample_format="F32LE"),
        content_hash="sha256:" + "0" * 64,
    )
    descriptor.time_range.start_ms = 0
    descriptor.time_range.end_ms = 5_000
    segment = media_pb2.AudioSegment(
        segment_id=descriptor.buffer_id,
        sample_rate=48_000,
        channels=2,
        bytes=960_000,
        partial=False,
        sample_format="F32LE",
    )

    parsed = common_pb2.BufferDescriptor.FromString(descriptor.SerializeToString())
    assert parsed.format.sample_format == "F32LE"
    assert parsed.format.sample_rate == 48_000 and parsed.format.channels == 2
    assert media_pb2.AudioSegment.FromString(segment.SerializeToString()).sample_format == "F32LE"
