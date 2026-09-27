"""对一份已授权的本地媒体文件做 replay，并校验 descriptor 报告。

真正的媒体验收需要显式授权的样本。合成片段仅用于跑通管道，绝不作为验收证据
（见 AGENTS.md）。
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
# 跨进程交接已实现（证据见 `make handoff-check`），因此这里不再要求任何 blocker；
# 但一次**不带** --handoff-listen 的 replay 并没有消费方，报告必须如实说明。
EXPECTED_BLOCKERS: set[str] = set()
EXPECTED_HANDOFF_STATE = "not_exercised"
DECODE_BLOCKER = "gstreamer_decode_not_implemented"
ADMITTED_MEMORY_KINDS = {"cpu_shared_memory"}
# pipeline 文件里的队列声明。它是报告里 `declared_capacity` 的唯一来源，
# 因此这里用与实现同样的正则在同一份文件上重读一遍，而不是相信报告自报的数。
QUEUE_CAPACITY = re.compile(r"^\s*queue_capacity:\s*(\d+)\s*$", re.MULTILINE)
# 两条独立实现描述同一段 presentation 时间轴：ffprobe 的 anchor 与 GStreamer 的
# decode 路径。毫秒级舍入可能不一致，因此区间比较时保留一定 slack。
TIMELINE_TOLERANCE_MS = 1
# 语义覆盖账本里允许出现的枚举取值。报告自报一个未知值就是契约漂移，必须当场失败。
EVIDENCE_DECISIONS = {
    "first_frame",
    "content_change",
    "text_change",
    "static_heartbeat",
    "no_change",
}
EVIDENCE_SELECTIONS = {
    "baseline_anchor",
    "event_anchor",
    "event_pre_context",
    "event_post_context",
    "not_selected",
}
EVIDENCE_TRIGGERS = {"content_change", "text_change"}
EVIDENCE_SKIP_REASONS = {
    "no_change_yet",
    "event_rate_limited",
    "event_budget_exhausted",
    "missing_signature",
    "non_monotonic_pts",
}
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
# these mirror crates/media/src/sampler.rs；报告必须落在实现真正接受的范围内，
# 因此这里做的是"策略自洽"检查，而不是给实现留余量。
MIN_INTERVAL_LOWER_MS = 100
MAX_INTERVAL_MS = 60_000
MAX_CHANGE_THRESHOLD = 128
# 抽帧跳过原因的权威名称来自 sampler.rs 的 SkipReason::name()。
SAMPLER_SKIP_COUNTERS = {
    "rate_limited": "skipped_rate_limited",
    "no_change_yet": "skipped_no_change",
    "non_monotonic_pts": "skipped_non_monotonic",
    "missing_signature": "skipped_missing_signature",
}


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
    assert report.handoff_state == EXPECTED_HANDOFF_STATE, (
        "a replay without --handoff-listen must report the lease consumer as not exercised, "
        f"got {report.handoff_state!r}"
    )
    assert str(media).encode() not in report.SerializeToString(), (
        "the report must not carry the media path"
    )


def check_media_queue(report, pipeline: Path) -> None:
    """分级准入字段必须自洽（ADR-019）。

    越界的运行在准入阶段就失败、根本写不出报告，所以"有上限却不等于已准入"这种组合
    不可能成立：这里检查的是报告与 pipeline 文件、与注入与否三者对得上。
    """

    match = QUEUE_CAPACITY.search(pipeline.read_text(encoding="utf-8"))
    assert match is not None, f"{pipeline} must declare queue_capacity"
    declared = int(match.group(1))
    admission = report.media_queue
    assert admission.declared_capacity == declared, (
        f"report declares {admission.declared_capacity} but {pipeline.name} says {declared}"
    )
    assert admission.retained_limit >= 1, "the retained window is a real queue, never zero"
    if admission.tier_capacity == 0:
        assert admission.state == "not_injected", admission.state
        assert admission.tier == "", "no tier was injected, so no tier name may be reported"
    else:
        assert admission.state == "admitted", admission.state
        assert admission.tier != "", "an injected cap must come with its tier name"
        assert admission.declared_capacity <= admission.tier_capacity, (
            "an exceeded declaration never reaches a report"
        )
        assert admission.retained_limit <= admission.tier_capacity


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
    assert decoded.decoded_bytes == sum(track.bytes for track in decoded.tracks), (
        "decoded byte totals disagree"
    )

    anchors_by_track: dict[str, list] = {}
    for anchor in report.anchors:
        anchors_by_track.setdefault(anchor.track_kind, []).append(anchor)

    sampling = check_sampling(decoded, anchors_by_track)
    # 被抽帧跳过的帧仍然是"解码到的一帧"：samples 只数交接，跳过数必须补回来，
    # 才能与 ffprobe 口径的锚点计数对齐。若两者不等，说明有一条路径漏计了帧。
    # 语义覆盖会额外交接两类帧：被采样器跳过后又作为事件证据交接的帧，以及先被丢弃、
    # 后被追认成"变化前上下文"的帧。它们让 `samples` 相对锚点数多算一次，多出来的部分
    # 恰好是 `kept_evidence_window`；没有语义计划时它是 0，等式退化成原来的口径。
    extra_handoffs = sampling["evidence_window"]
    assert samples + sampling["skipped"] == report.decoded_items + extra_handoffs, (
        f"handed off {samples} + sampled out {sampling['skipped']} != "
        f"decoded items {report.decoded_items} + evidence-window keeps {extra_handoffs}"
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

    offsets = {}
    for track in decoded.tracks:
        offsets[track.track_kind] = check_track(
            track, report.source.duration_ms, anchors_by_track.get(track.track_kind, [])
        )

    check_segments(decoded, anchors_by_track)
    check_evidence(decoded)
    check_semantic_coverage(decoded, sampling)
    return offsets


def check_sampling(decoded, anchors_by_track: dict) -> dict:
    """抽帧必须真的运行，且观测数、保留数与跳过原因必须自洽。

    抽帧不是"尽力而为"的优化：报告里没有它，或它的计数自相矛盾，
    都等于拿一个无法复核的样本集当结果。
    """
    video = next((track for track in decoded.tracks if track.track_kind == "video"), None)
    if video is None:
        assert not decoded.sampling, "a track that was never decoded cannot report sampling"
        assert not decoded.semantic_coverage, "no video track means no semantic coverage ledger"
        return {
            "skipped": 0,
            "evidence_window": 0,
            "observed": 0,
            "kept": 0,
            "max_frame_interval_ms": 0,
            "max_gap_ms": 0,
        }
    assert decoded.sampling, "a decoded video track must report its sampling accounting"
    assert len(decoded.sampling) == 1, "only the video track is sampled"
    sampling = decoded.sampling[0]
    assert sampling.track_kind == "video", sampling.track_kind

    assert MIN_INTERVAL_LOWER_MS <= sampling.min_interval_ms <= MAX_INTERVAL_MS, (
        f"min_interval_ms {sampling.min_interval_ms} is outside the accepted range"
    )
    assert sampling.static_hold_ms >= sampling.min_interval_ms, (
        "a static hold shorter than the rate limit could never fire"
    )
    assert 1 <= sampling.change_threshold <= MAX_CHANGE_THRESHOLD, (
        f"change_threshold {sampling.change_threshold} is out of range"
    )

    skipped = sum(getattr(sampling, field) for field in SAMPLER_SKIP_COUNTERS.values())
    assert sampling.observed == sampling.kept + skipped, (
        f"observed {sampling.observed} != kept {sampling.kept} + skipped {skipped}"
    )
    assert sampling.observed > 0, "the sampler saw no frame although the video track decoded"
    assert sampling.kept == (
        sampling.kept_first_frame + sampling.kept_content_change + sampling.kept_static_heartbeat
    ), "every keep needs exactly one reason"
    # 语义覆盖会额外交接事件窗口帧与预上下文帧；没有语义计划时这一项恒为 0，
    # 于是等式退化成原来的"采样保留 == 交接帧数"。
    assert sampling.kept + sampling.kept_evidence_window == video.samples, (
        f"sampling kept {sampling.kept} + evidence-window keeps "
        f"{sampling.kept_evidence_window} but the video track handed off {video.samples}"
    )
    # 两条独立路径必须看到同样多的视频帧：ffprobe 的锚点数与解码器交给采样器的帧数。
    # 对不上就说明有一侧漏了帧，"覆盖率"也就无从谈起。
    video_anchors = anchors_by_track.get("video", [])
    assert len(video_anchors) == sampling.observed, (
        f"the probe reported {len(video_anchors)} video anchors but the decoder sampled "
        f"{sampling.observed} frames"
    )
    if decoded.semantic_coverage:
        # 预上下文帧先被丢弃、后被追认交接，因此在"丢弃"与"交接"两边都会出现。
        # 有语义计划时只能要求每个观测帧都被解释成其中至少一种，不能要求两边互斥。
        assert video.dropped_samples + video.samples >= sampling.observed, (
            f"dropped {video.dropped_samples} + handed off {video.samples} "
            f"< observed {sampling.observed}"
        )
    else:
        # 抽帧跳过也计入轨道的丢弃总数：跳过不是"没解码到这一帧"，而是"没交接这一帧"。
        assert video.dropped_samples >= skipped, (
            f"track dropped {video.dropped_samples} < sampler skipped {skipped}"
        )
    for reason, field in SAMPLER_SKIP_COUNTERS.items():
        count = getattr(sampling, field)
        if count:
            assert reason in video.drop_reasons, (
                f"{count} frames skipped as {reason} without a reason on the track"
            )
    assert sampling.kept <= sampling.max_keeps_bound, (
        f"kept {sampling.kept} exceeds the rate bound {sampling.max_keeps_bound}"
    )
    # 静止段不会无限期不采样：一次 keep 之后最多再等 hold + 一个观测到的帧间隔。
    assert sampling.max_gap_ms <= (
        sampling.static_hold_ms + sampling.max_frame_interval_ms + TIMELINE_TOLERANCE_MS
    ), (
        f"max gap {sampling.max_gap_ms}ms is not bounded by hold "
        f"{sampling.static_hold_ms}ms + frame interval {sampling.max_frame_interval_ms}ms"
    )
    return {
        "skipped": skipped,
        "evidence_window": sampling.kept_evidence_window,
        "observed": sampling.observed,
        "kept": sampling.kept,
        "max_frame_interval_ms": sampling.max_frame_interval_ms,
        "max_gap_ms": sampling.max_gap_ms,
    }


def parse_counter_entries(entries, label: str) -> dict:
    """把 `name=count` 形式的聚合计数解析成字典；解析不了就失败，不做兜底。"""
    parsed: dict[str, int] = {}
    for entry in entries:
        name, separator, count = entry.partition("=")
        assert separator == "=" and name, f"{label} entry {entry!r} is not name=count"
        assert name not in parsed, f"{label} repeats {name}"
        parsed[name] = int(count)
    return parsed


def check_semantic_coverage(decoded, sampling: dict) -> None:
    """语义覆盖账本必须同时证明两件事：输入完整性与语义刷新上界。

    它和采样报告描述同一批帧，因此判别帧数必须等于采样器观测到的帧数；每个被判别帧都要
    有结论（选中或带原因被覆盖）；静态画面的相邻语义输入不得超过 `max_semantic_gap_ms`
    加一个观测到的帧间隔。窗口只引用这次运行真的交接过的 buffer，绝不携带字节。
    """
    if not decoded.semantic_coverage:
        assert sampling["evidence_window"] == 0, (
            "kept_evidence_window is non-zero but there is no semantic coverage report"
        )
        return
    assert len(decoded.semantic_coverage) == 1, "only the video track carries a coverage ledger"
    coverage = decoded.semantic_coverage[0]
    assert coverage.track_kind == "video", coverage.track_kind

    selected = (
        coverage.selected_baseline_anchor
        + coverage.selected_event_anchor
        + coverage.selected_event_pre_context
        + coverage.selected_event_post_context
    )
    assert coverage.characterized_frames == sampling["observed"], (
        f"coverage characterized {coverage.characterized_frames} frames but the sampler "
        f"observed {sampling['observed']}"
    )
    assert coverage.characterized_frames > 0, "a decoded video track must characterize its frames"
    assert selected + coverage.covered_without_model_refresh == coverage.characterized_frames, (
        "every characterized frame must be either selected or explicitly covered"
    )
    assert selected >= sampling["evidence_window"], (
        f"selected {selected} < evidence-window keeps {sampling['evidence_window']}"
    )

    assert 100 <= coverage.max_semantic_gap_ms <= 60_000, coverage.max_semantic_gap_ms
    assert coverage.evidence_pre_frames <= coverage.evidence_context_frames, (
        "pre context cannot exceed the whole context window"
    )
    post_frames = coverage.evidence_context_frames - coverage.evidence_pre_frames
    frame_interval = sampling["max_frame_interval_ms"]
    assert coverage.max_selected_gap_ms <= (
        coverage.max_semantic_gap_ms + frame_interval + TIMELINE_TOLERANCE_MS
    ), (
        f"max selected gap {coverage.max_selected_gap_ms}ms exceeds the policy bound "
        f"{coverage.max_semantic_gap_ms}ms + frame interval {frame_interval}ms"
    )

    decisions = parse_counter_entries(coverage.decision_counts, "decision_counts")
    selections = parse_counter_entries(coverage.selection_counts, "selection_counts")
    skips = parse_counter_entries(coverage.skip_reason_counts, "skip_reason_counts")
    assert set(decisions) <= EVIDENCE_DECISIONS, decisions
    assert set(selections) <= EVIDENCE_SELECTIONS, selections
    assert set(skips) <= EVIDENCE_SKIP_REASONS, skips
    assert sum(decisions.values()) == coverage.characterized_frames, (
        "decision counters must cover every characterized frame"
    )
    assert sum(selections.values()) == coverage.characterized_frames, (
        "selection counters must cover every characterized frame"
    )
    assert sum(skips.values()) == coverage.covered_without_model_refresh, (
        "skip reasons must account for exactly the covered-without-refresh frames"
    )
    assert selections.get("baseline_anchor", 0) == coverage.selected_baseline_anchor
    assert selections.get("event_anchor", 0) == coverage.selected_event_anchor
    assert selections.get("event_pre_context", 0) == coverage.selected_event_pre_context
    assert selections.get("event_post_context", 0) == coverage.selected_event_post_context
    assert selections.get("not_selected", 0) == coverage.covered_without_model_refresh

    assert len(coverage.listed_windows) == min(coverage.windows, coverage.windows_listed_limit), (
        "listed windows must be the bounded preview of the window count"
    )
    window_ids = set()
    for window in coverage.listed_windows:
        assert window.window_id, "a window must carry its identifier"
        assert window.window_id not in window_ids, f"duplicate window id {window.window_id}"
        window_ids.add(window.window_id)
        assert window.trigger in EVIDENCE_TRIGGERS, window.trigger
        assert window.context_before <= coverage.evidence_pre_frames
        assert window.context_after <= post_frames
        assert window.time_range.start_ms <= window.anchor_ms < window.time_range.end_ms, (
            f"window {window.window_id} anchor is outside its half-open interval"
        )
        assert len(window.frame_buffer_ids) == window.handed_off_frames, (
            "handed_off_frames must equal the number of buffer references"
        )
        assert window.handed_off_frames <= (window.context_before + 1 + window.context_after), (
            "a window cannot hold more frames than its bounded context allows"
        )
    check_frame_ledger(coverage)


def check_frame_ledger(coverage) -> None:
    """逐帧账本是"输入完整性"的落盘证据：每一帧恰好一行，且聚合计数必须与它一致。

    没有账本路径时不能声称有账本条目；给了路径但文件读不到、行数对不上、聚合计数对不上，
    都让验收失败——否则"每一帧都有记录"只是一句无法复核的话。
    """
    path = coverage.frame_ledger_path
    if not path:
        assert coverage.frame_ledger_entries == 0, (
            "a report without a ledger path cannot claim ledger entries"
        )
        return
    ledger = Path(path)
    assert ledger.is_file(), f"the reported frame ledger {path} does not exist"
    frames: list[dict] = []
    windows: list[dict] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record_type") == "frame":
            frames.append(record)
        elif record.get("record_type") == "window":
            windows.append(record)
        else:
            raise AssertionError(f"unknown ledger record_type {record.get('record_type')!r}")

    assert len(frames) == coverage.frame_ledger_entries, (
        f"ledger holds {len(frames)} frames but the report claims "
        f"{coverage.frame_ledger_entries} entries"
    )
    assert len(frames) == coverage.characterized_frames, (
        f"ledger holds {len(frames)} frames but the report characterized "
        f"{coverage.characterized_frames}"
    )
    indexes = [frame["frame_index"] for frame in frames]
    assert indexes == list(range(1, len(frames) + 1)), (
        "the ledger must record every frame exactly once, in order"
    )
    assert len(windows) == coverage.windows, (
        f"ledger holds {len(windows)} windows but the report claims {coverage.windows}"
    )

    decisions = Counter(frame["decision"] for frame in frames)
    selections = Counter(frame["selection"] for frame in frames)
    skips = Counter(frame["skip_reason"] for frame in frames if frame["skip_reason"])
    assert dict(decisions) == parse_counter_entries(coverage.decision_counts, "decision_counts")
    assert dict(selections) == parse_counter_entries(coverage.selection_counts, "selection_counts")
    # 账本里出现 `data_plane_retention_rejected` 时，说明这一帧是被数据面容量拒绝的，
    # 不是被证据计划跳过的，因此它不是聚合跳过计数的一部分。
    known_skips = {
        reason: count for reason, count in skips.items() if reason in EVIDENCE_SKIP_REASONS
    }
    assert set(skips) - EVIDENCE_SKIP_REASONS <= {"data_plane_retention_rejected"}, skips
    assert known_skips == parse_counter_entries(coverage.skip_reason_counts, "skip_reason_counts")

    # 未选中的帧绝不能带 buffer 引用：账本里的"没选中"和"交接了"不能同时成立。
    for frame in frames:
        if frame["selection"] == "not_selected":
            assert not frame["buffer_id"], (
                f"frame {frame['frame_index']} is not selected yet carries {frame['buffer_id']}"
            )
    handed = {frame["buffer_id"] for frame in frames if frame["buffer_id"]}
    for window in windows:
        for buffer_id in window["frame_buffer_ids"]:
            assert buffer_id in handed, (
                f"window {window['window_id']} references {buffer_id} which is not in the ledger"
            )
    for window, listed in zip(windows, coverage.listed_windows, strict=True):
        assert window["window_id"] == listed.window_id
        assert window["frame_buffer_ids"] == list(listed.frame_buffer_ids)


def describe_sampling(decoded) -> str:
    if not decoded.sampling:
        return "sampling=none"
    sampling = decoded.sampling[0]
    skipped = ", ".join(
        f"{reason}={getattr(sampling, field)}"
        for reason, field in SAMPLER_SKIP_COUNTERS.items()
        if getattr(sampling, field)
    )
    kept = (
        f"first_frame={sampling.kept_first_frame},"
        f"content_change={sampling.kept_content_change},"
        f"static_heartbeat={sampling.kept_static_heartbeat}"
    )
    evidence = ""
    if decoded.semantic_coverage:
        coverage = decoded.semantic_coverage[0]
        selected = (
            coverage.selected_baseline_anchor
            + coverage.selected_event_anchor
            + coverage.selected_event_pre_context
            + coverage.selected_event_post_context
        )
        evidence = (
            f" evidence=characterized {coverage.characterized_frames} "
            f"selected {selected} "
            f"covered {coverage.covered_without_model_refresh} "
            f"windows={coverage.windows} max_selected_gap_ms={coverage.max_selected_gap_ms}"
        )
    return (
        f"sampling=kept {sampling.kept}/{sampling.observed} ({kept}) "
        f"skipped[{skipped or 'none'}] max_gap_ms={sampling.max_gap_ms} "
        f"bound={sampling.max_keeps_bound} evidence_windows={sampling.kept_evidence_window}"
        f"{evidence}"
    )


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
        "--without-evidence",
        action="store_true",
        help="replay without the semantic-coverage plan (only checks the adaptive sampler)",
    )
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
        finish(report, media, args.pipeline)
        return
    with tempfile.TemporaryDirectory() as workspace:
        report_path = args.report or Path(workspace) / "replay-report.pb"
        evidence_flags = []
        if not args.without_evidence:
            # 全帧判别是这份验收的一部分：没有它就只验了抽帧速率，验不到语义覆盖。
            evidence_flags = [
                "--evidence",
                "--evidence-ledger",
                str(Path(workspace) / "replay.evidence.jsonl"),
            ]
        subprocess.run(
            [
                str(runtime_binary()),
                "replay",
                str(args.pipeline),
                str(media),
                "--report",
                str(report_path),
                *evidence_flags,
            ],
            check=True,
        )
        report = parse_report(report_path)
        # 账本检查要按报告里的路径重新读文件，所以必须在临时工作目录还存在时完成校验，
        # 否则"每一帧都有落盘记录"会被误判成"账本不存在"。
        finish(report, media, args.pipeline)


def finish(report, media: Path, pipeline: Path) -> None:
    check_source(report, media)
    check_anchors(report)
    check_honesty(report, media)
    check_media_queue(report, pipeline)
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
        f"{describe_sampling(decoded)} "
        f"queue_capacity={report.media_queue.state}/declared={report.media_queue.declared_capacity}"
        f"/tier_capacity={report.media_queue.tier_capacity}"
        f"/retained_limit={report.media_queue.retained_limit} "
        f"drop_reasons={','.join(report.drop_reasons) or 'none'} "
        f"blockers={','.join(report.blockers)}"
    )


if __name__ == "__main__":
    main()
