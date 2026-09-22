"""Contract checks for the media source and replay report messages."""

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
