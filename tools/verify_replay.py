"""Replay one authorized local media file and verify the descriptor report.

Real media acceptance requires an explicitly authorized sample. A synthetic clip only
exercises the plumbing and is never acceptance evidence (see AGENTS.md).
"""

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "target/debug/sensoryplex-runtime"
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
EXPECTED_BLOCKERS = {
    "gstreamer_decode_not_implemented",
    "buffer_lease_handoff_not_implemented",
}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def parse_report(report_path: Path) -> media_pb2.ReplayReport:
    report = media_pb2.ReplayReport()
    report.ParseFromString(report_path.read_bytes())
    return report


def check_source(report, media: Path) -> None:
    source = report.source.source
    assert source.kind == media_pb2.MEDIA_SOURCE_KIND_FILE, source.kind
    assert source.content_hash == file_digest(media), "report digest does not match the file"
    assert source.stream_id and source.source_id, "stream and source identity must be reported"
    assert report.source.probe_tool.startswith("ffprobe"), report.source.probe_tool
    assert report.source.duration_ms > 0, "file replays must report a known duration"
    assert report.source.tracks, "no tracks were probed"
    for track in report.source.tracks:
        assert track.track_kind in {"video", "audio"}, track.track_kind
        assert track.codec, f"{track.track_kind} codec is unknown"


def check_anchors(report) -> None:
    duration_ms = report.source.duration_ms
    by_track: dict[str, list] = {}
    for anchor in report.anchors:
        by_track.setdefault(anchor.track_kind, []).append(anchor)
    assert by_track, "no timeline anchors were produced"
    for track_kind, anchors in by_track.items():
        previous_start = -1
        for anchor in anchors:
            start, end = anchor.time_range.start_ms, anchor.time_range.end_ms
            assert start >= 0 and end > start, f"{track_kind} anchor is not half-open"
            assert start > previous_start, f"{track_kind} anchors are not strictly increasing"
            assert anchor.pts_ms == start, f"{track_kind} pts must open its interval"
            assert end <= duration_ms, f"{track_kind} anchor exceeds the reported duration"
            previous_start = start
    assert report.emitted_anchors == len(report.anchors)
    assert report.decoded_items == report.emitted_anchors + report.dropped_items


def check_honesty(report, media: Path) -> None:
    assert not report.golden_path_verified, "this build cannot claim the golden path"
    assert EXPECTED_BLOCKERS <= set(report.blockers), report.blockers
    assert str(media).encode() not in report.SerializeToString(), (
        "the report must not carry the media path"
    )


def describe_track(track) -> str:
    if track.track_kind == "video":
        return f"{track.track_kind}:{track.codec}@{track.width}x{track.height}"
    return f"{track.track_kind}:{track.codec}@{track.sample_rate}Hz/{track.channels}ch"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path, default=PIPELINE)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    media = args.media.resolve()
    if not media.is_file():
        raise SystemExit(f"not a media file: {media}")
    with tempfile.TemporaryDirectory() as workspace:
        report_path = args.report or Path(workspace) / "replay-report.pb"
        subprocess.run(
            [
                str(RUNTIME),
                "replay",
                str(args.pipeline),
                str(media),
                "--report",
                str(report_path),
            ],
            check=True,
        )
        report = parse_report(report_path)
    check_source(report, media)
    check_anchors(report)
    check_honesty(report, media)
    tracks = ", ".join(describe_track(track) for track in report.source.tracks)
    print(
        f"Replay verified: platform={report.platform} duration_ms={report.source.duration_ms} "
        f"tracks=[{tracks}] anchors={report.emitted_anchors} dropped={report.dropped_items} "
        f"gaps={report.gap_items} reordered={report.out_of_order_items} "
        f"drop_reasons={','.join(report.drop_reasons) or 'none'} "
        f"blockers={','.join(report.blockers)}"
    )


if __name__ == "__main__":
    main()
