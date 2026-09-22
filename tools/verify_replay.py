"""对一份已授权的本地媒体文件做 replay，并校验 descriptor 报告。

真正的媒体验收需要显式授权的样本。合成片段仅用于跑通管道，绝不作为验收证据
（见 AGENTS.md）。
"""

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
EXPECTED_BLOCKERS = {
    "adaptive_sampling_not_implemented",
    "lease_consumer_not_implemented",
}
DECODE_BLOCKER = "gstreamer_decode_not_implemented"
ADMITTED_MEMORY_KINDS = {"cpu_shared_memory"}
# 两条独立实现描述同一段 presentation 时间轴：ffprobe 的 anchor 与 GStreamer 的
# decode 路径。毫秒级舍入可能不一致，因此区间比较时保留一定 slack。
TIMELINE_TOLERANCE_MS = 1
# Codec pre-skip（Opus `initial_padding=312` = 6.5 ms）在流的最开始两端放置方式不同，
# 因此第一个音频样本最多可以相差一帧 codec 帧（Opus 为 20 ms）。该偏移会作为一项
# 信息输出，而不是被强行 assert 掉；像 166 ms edit-list 那样的真实错位会远大于此，
# 仍会被本检查捕获。
VIDEO_START_TOLERANCE_MS = 1
AUDIO_START_TOLERANCE_MS = 25
# Segment 按样本游标切分，但报告中的起始时间是该样本所在帧的 pts。
# Matroska 对 20 ms Opus 帧按毫秒存储 pts，因此该 pts 可能落在上一 segment 结束前
# 或结束后一个 codec 帧的距离内。真正的空洞已经由 segmenter 的 250 ms discontinuity
# 规则隔开，因此这段 slack 不会掩盖真实空洞。
SEGMENT_BOUNDARY_TOLERANCE_MS = 25


def runtime_binary() -> Path:
    """优先使用 Makefile 产出的 release 构建；debug 构建也接受。"""
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make media-replay builds it for you")


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


def check_decoded_plane(report) -> None:
    """校验已解码的 data plane，或确认仅 anchor 构建没有越界声明。"""
    decoded = report.decoded
    if DECODE_BLOCKER in set(report.blockers):
        assert not decoded.arena_id, "an anchors-only build must not report an arena"
        assert not decoded.tracks, "an anchors-only build must not report decoded tracks"
        assert not decoded.evidence_descriptors, "an anchors-only build must not report buffers"
        assert decoded.descriptors_built == 0 and decoded.leases_issued == 0
        return {}

    assert decoded.arena_id and "/" not in decoded.arena_id, "arena id must be an opaque handle"
    assert decoded.arena_capacity_bytes > 0, "the arena must have a bounded capacity"
    assert decoded.arena_peak_bytes <= decoded.arena_capacity_bytes, "arena capacity was exceeded"
    assert decoded.tracks, "a decoding build must report per-track statistics"

    samples = sum(track.samples for track in decoded.tracks)
    assert samples == report.decoded_items, "decoded samples must match the item count"
    assert decoded.decoded_bytes == sum(track.bytes for track in decoded.tracks), (
        "decoded byte totals disagree"
    )

    # 每个被接受的样本恰好生成一个 buffer，每段音频额外生成一个 segment。
    expected_descriptors = samples + decoded.audio_segments.segments
    assert decoded.descriptors_built == expected_descriptors, (
        f"descriptor count {decoded.descriptors_built} != samples + segments {expected_descriptors}"
    )
    assert decoded.descriptors_validated == decoded.descriptors_built, (
        "descriptors were not verified"
    )
    assert decoded.descriptor_failures == 0, decoded.failure_reasons
    assert not decoded.failure_reasons, decoded.failure_reasons
    assert decoded.leases_released == decoded.leases_issued, "leases leaked"
    assert decoded.leases_issued == decoded.descriptors_built, "every buffer needs one lease"

    anchors_by_track: dict[str, list] = {}
    for anchor in report.anchors:
        anchors_by_track.setdefault(anchor.track_kind, []).append(anchor)
    offsets = {}
    for track in decoded.tracks:
        offsets[track.track_kind] = check_track(
            track, report.source.duration_ms, anchors_by_track.get(track.track_kind, [])
        )

    check_segments(decoded, anchors_by_track)
    check_evidence(decoded)
    return offsets


def check_track(track, duration_ms: int, anchors: list) -> int:
    label = track.track_kind
    assert label in {"video", "audio"}, label
    assert track.samples > 0 and track.bytes > 0, f"{label} decoded nothing"
    if track.dropped_samples:
        assert track.drop_reasons, f"{label} dropped samples without a reason"
    assert 0 <= track.first_pts_ms <= duration_ms, f"{label} first pts is outside the source"
    assert track.first_pts_ms < track.last_end_ms <= duration_ms, (
        f"{label} interval is not half-open"
    )
    if label == "video":
        assert track.width > 0 and track.height > 0 and track.pixel_format, (
            "video layout is unknown"
        )
    else:
        assert track.sample_rate > 0 and track.channels > 0 and track.audio_format, (
            "audio layout is unknown"
        )
    # decode 路径以 presentation origin 为基准重定位；probe 端 anchor 也是如此，
    # 因此两端必须在第一个样本位置与最后一个样本收尾上保持一致。
    assert anchors, f"{label} has decoded samples but no timeline anchors"
    first = anchors[0].time_range.start_ms
    start_tolerance = VIDEO_START_TOLERANCE_MS if label == "video" else AUDIO_START_TOLERANCE_MS
    assert abs(track.first_pts_ms - first) <= start_tolerance, (
        f"{label} decode starts at {track.first_pts_ms}ms but the anchors start at {first}ms"
    )
    last = anchors[-1].time_range
    assert last.start_ms <= track.last_end_ms <= last.end_ms, (
        f"{label} decode ends at {track.last_end_ms}ms, outside the final anchor interval"
    )
    return track.first_pts_ms - first


def check_segments(decoded, anchors_by_track: dict) -> None:
    report = decoded.audio_segments
    audio = next((track for track in decoded.tracks if track.track_kind == "audio"), None)
    assert report.segment_ms > 0, "audio segmenting was not configured"
    if audio is None:
        assert report.segments == 0 and not report.listed, "segments without an audio track"
        return
    assert report.segments > 0, "audio samples were decoded but never segmented"
    assert report.dropped_samples == 0 or report.drop_reasons, (
        "dropped audio samples must carry a reason"
    )
    assert report.segment_ms <= audio.last_end_ms, "the segment length exceeds the decoded audio"
    if report.listed == report.segments:
        listed = sum(segment.bytes for segment in report.listed)
        assert listed == report.bytes == audio.bytes, (
            "segment bytes do not add up to the audio bytes"
        )
        previous = None
        for index, segment in enumerate(report.listed, start=1):
            start, end = segment.time_range.start_ms, segment.time_range.end_ms
            assert start < end, f"segment {index} is not half-open"
            assert segment.bytes > 0 and segment.sample_rate == audio.sample_rate
            assert end - start <= report.segment_ms + TIMELINE_TOLERANCE_MS, (
                f"segment {index} is longer than the configured length"
            )
            if previous is not None:
                assert abs(start - previous) <= SEGMENT_BOUNDARY_TOLERANCE_MS, (
                    f"segment {index} does not continue the previous segment"
                )
            previous = end
        tail = report.listed[-1]
        assert tail.partial, "the short final segment must be flagged partial"
        assert tail.time_range.end_ms == audio.last_end_ms, "the final segment is short"


def check_evidence(decoded) -> None:
    kinds = set()
    for descriptor in decoded.evidence_descriptors:
        label = descriptor.kind
        kinds.add(label)
        assert descriptor.memory_kind in ADMITTED_MEMORY_KINDS, descriptor.memory_kind
        handle = descriptor.locator.handle
        assert handle == decoded.arena_id and "/" not in handle, (
            "the locator must reference the arena, never a host path"
        )
        assert descriptor.locator.length > 0, "an empty buffer was admitted"
        assert descriptor.locator.offset + descriptor.locator.length <= decoded.arena_capacity_bytes
        assert descriptor.stream_id, "a buffer without a stream identity is unusable"
        start = descriptor.time_range.start_ms
        end = descriptor.time_range.end_ms
        assert 0 <= start < end, f"{label} evidence is not half-open"
        assert descriptor.lease.read_only, "batch consumers get read-only leases"
        assert descriptor.lease.lease_id and descriptor.lease.expires_at_unix_ms > 0, (
            "an evidence lease must be explicit"
        )
        assert (
            descriptor.content_hash.startswith("sha256:") and len(descriptor.content_hash) == 71
        ), descriptor.content_hash
        if label == "video_frame":
            assert descriptor.format.width > 0 and descriptor.format.height > 0
    if decoded.tracks:
        assert kinds, "a decoding build must keep at least one evidence buffer"


def describe_track(track) -> str:
    if track.track_kind == "video":
        return f"{track.track_kind}:{track.codec}@{track.width}x{track.height}"
    return f"{track.track_kind}:{track.codec}@{track.sample_rate}Hz/{track.channels}ch"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path, default=PIPELINE)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check an existing --report instead of replaying the media again",
    )
    args = parser.parse_args()
    media = args.media.resolve()
    if not media.is_file():
        raise SystemExit(f"not a media file: {media}")
    if args.verify_only:
        if not args.report:
            raise SystemExit("--verify-only needs --report pointing at an existing report")
        report = parse_report(args.report)
        finish(report, media)
        return
    with tempfile.TemporaryDirectory() as workspace:
        report_path = args.report or Path(workspace) / "replay-report.pb"
        subprocess.run(
            [
                str(runtime_binary()),
                "replay",
                str(args.pipeline),
                str(media),
                "--report",
                str(report_path),
            ],
            check=True,
        )
        report = parse_report(report_path)
    finish(report, media)


def finish(report, media: Path) -> None:
    check_source(report, media)
    check_anchors(report)
    check_honesty(report, media)
    offsets = check_decoded_plane(report)
    decoded = report.decoded
    tracks = ", ".join(describe_track(track) for track in report.source.tracks)
    print(
        f"Replay verified: platform={report.platform} duration_ms={report.source.duration_ms} "
        f"tracks=[{tracks}] anchors={report.emitted_anchors} dropped={report.dropped_items} "
        f"gaps={report.gap_items} reordered={report.out_of_order_items} "
        f"descriptors={decoded.descriptors_built} leases={decoded.leases_issued}"
        f"/{decoded.leases_released} segments={decoded.audio_segments.segments} "
        f"arena_peak_bytes={decoded.arena_peak_bytes} "
        f"start_offsets=[{','.join(f'{kind}:{value:+d}ms' for kind, value in offsets.items())}] "
        f"drop_reasons={','.join(report.drop_reasons) or 'none'} "
        f"blockers={','.join(report.blockers)}"
    )


if __name__ == "__main__":
    main()
