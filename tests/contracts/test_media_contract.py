"""media source 与 replay report 消息的契约校验。"""

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
