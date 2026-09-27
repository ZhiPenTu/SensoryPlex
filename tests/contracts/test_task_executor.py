"""受控 v2 执行器的纯契约：不启动媒体或模型进程。

这里固定的是"全帧判别 + 有界证据窗口"的**输入单位**与**诚实降级**语义：Runtime 决定哪些帧
被选中，执行器只按模态把它们翻译成有界的 `Process` 请求；被有界数据面拒绝的单位必须显式
登记成 `data_plane_retention_rejected` 并跳过，而不是让整条 Task 失败，也不是被读成"画面没变"。
"""

import json
from types import SimpleNamespace

import pytest
from edge_material_sdk.generated.media.v1 import media_pb2

from tools.task_executor import (
    RETENTION_REJECTED,
    TaskExecutionError,
    TaskExecutor,
    _bounded_policy,
    _evidence_plan,
    _EvidenceStream,
    _InputGroup,
    _modality_view,
    _refused_records,
    _RejectedUnit,
    _runtime_pipeline,
)


def policy(**overrides) -> dict:
    base = {
        "window_ms": 1000,
        "sample_interval_ms": 1000,
        "audio_segment_ms": 6000,
        "audio_overlap_ms": 500,
        "vlm_sample_interval_ms": 5000,
    }
    return {**base, **overrides}


def _source(duration_ms: int, kinds: tuple[str, ...] = ("video",)):
    return SimpleNamespace(
        source=SimpleNamespace(
            duration_ms=duration_ms,
            tracks=[SimpleNamespace(track_kind=kind) for kind in kinds],
        )
    )


def test_runtime_pipeline_does_not_declare_vlm_when_revision_has_no_vlm():
    without_vlm = _runtime_pipeline(policy(), has_vlm=False)
    with_vlm = _runtime_pipeline(policy(), has_vlm=True)

    assert "slow_enrichment: []" in without_vlm
    assert "- type: vlm" not in without_vlm
    assert "enrichment_modalities: []" in without_vlm
    assert "- type: vlm" in with_vlm
    assert "- vision.scene_description" in with_vlm


def test_empty_observations_still_produce_complete_one_second_coverage(tmp_path):
    executor = TaskExecutor(None, base_dir=tmp_path)
    # 没有语义计划（上游没有报告）时保持旧的保守口径：既不能声称采样过，也不能声称覆盖。
    coverage = executor._coverage(_source(9056), [], {}, policy(), [], [])

    assert [(item["start_ms"], item["end_ms"]) for item in coverage] == [
        (0, 1000),
        (1000, 2000),
        (2000, 3000),
        (3000, 4000),
        (4000, 5000),
        (5000, 6000),
        (6000, 7000),
        (7000, 8000),
        (8000, 9000),
        (9000, 9056),
    ]
    assert all(item["modality_states"]["ocr"] == "not_sampled_by_policy" for item in coverage)
    assert all(item["modality_states"]["asr"] == "not_applicable" for item in coverage)


def test_coverage_separates_covered_windows_from_missing_observations(tmp_path):
    # 第 1 秒内计划刷新过（锚点 1100ms），但插件没有返回任何观测：这是缺口，不是"没变"。
    selected = [(100, 133, "baseline_refresh"), (500, 533, "content_change")]
    coverage = TaskExecutor(None, base_dir=tmp_path)._coverage(
        _source(3000), [], {}, policy(), selected, []
    )

    assert coverage[0]["modality_states"]["ocr"] == "not_observed"
    assert coverage[0]["reason_codes"]["ocr"] == "selected_without_observation"
    assert coverage[0]["modality_states"]["vlm"] == "not_observed"
    assert coverage[0]["sampling_state"] == "semantic_refresh_without_observation"
    assert coverage[0]["evidence_triggers"] == ["baseline_refresh", "content_change"]
    # 第 3 秒没有任何选中帧：逐帧判别过，但计划本来就不在这里重新送模型。
    assert coverage[2]["modality_states"]["ocr"] == "covered_without_model_refresh"
    assert coverage[2]["reason_codes"]["ocr"] == "semantic_coverage"
    assert coverage[2]["sampling_state"] == "covered_without_model_refresh"


def test_coverage_names_retention_rejection_instead_of_a_generic_gap(tmp_path):
    # 计划在这一秒刷新过，但成本帧被有界保留面拒绝：原因码必须是数据面拒绝，
    # 不能降级成"刷新了却没有观测"这种泛化缺口。
    selected = [(1100, 1133, "content_change")]
    coverage = TaskExecutor(None, base_dir=tmp_path)._coverage(
        _source(2000), [], {}, policy(), selected, [(1100, 1133)]
    )

    assert coverage[1]["modality_states"]["ocr"] == "not_observed"
    assert coverage[1]["reason_codes"]["ocr"] == RETENTION_REJECTED
    assert coverage[1]["reason_codes"]["vlm"] == RETENTION_REJECTED
    assert coverage[1]["sampling_state"] == "semantic_refresh_without_observation"
    # 没有被拒绝帧覆盖的秒格仍然是"计划覆盖过"。
    assert coverage[0]["modality_states"]["ocr"] == "covered_without_model_refresh"


def _ledger(tmp_path, records) -> "object":
    path = tmp_path / "node.replay.evidence.jsonl"
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _frame(
    index: int,
    selection: str,
    *,
    buffer_id: str = "",
    decision: str = "no_change",
    window_id: str = "",
    skip_reason: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    return {
        "record_type": "frame",
        "buffer_id": buffer_id,
        "frame_index": index,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "selection": selection,
        "decision": decision,
        "window_id": window_id,
        "skip_reason": skip_reason,
    }


def _window(
    window_id: str,
    frame_buffer_ids,
    *,
    anchor_ms: int,
    trigger: str = "content_change",
    context_before: int = 0,
    context_after: int = 1,
    last_frame_index: int = 1,
) -> dict:
    return {
        "record_type": "window",
        "window_id": window_id,
        "start_ms": max(0, anchor_ms - 1000),
        "end_ms": anchor_ms + 1000,
        "anchor_ms": anchor_ms,
        "trigger": trigger,
        "frame_buffer_ids": list(frame_buffer_ids),
        "context_before": context_before,
        "context_after": context_after,
        "handed_off_frames": len(frame_buffer_ids),
        "last_frame_index": last_frame_index,
    }


def _coverage_report(characterized: int, listed, windows: int) -> media_pb2.ReplayReport:
    report = media_pb2.ReplayReport()
    coverage = report.decoded.semantic_coverage.add()
    coverage.track_kind = "video"
    coverage.characterized_frames = characterized
    coverage.covered_without_model_refresh = characterized - sum(
        len(window.frame_buffer_ids) for window in listed
    )
    coverage.windows = windows
    coverage.windows_listed_limit = 256
    coverage.listed_windows.extend(listed)
    coverage.frame_ledger_path = "/tmp/does-not-matter.jsonl"
    coverage.frame_ledger_entries = characterized
    return report


def test_evidence_plan_reads_every_frame_and_builds_modal_units(tmp_path):
    records = [
        _frame(1, "baseline_anchor", buffer_id="buf-1", decision="first_frame", end_ms=33),
        _frame(2, "not_selected", decision="no_change", skip_reason="no_change_yet", end_ms=66),
        _frame(
            3,
            "event_anchor",
            buffer_id="buf-3",
            decision="content_change",
            window_id="win-1",
            end_ms=100,
        ),
        _frame(
            4,
            "event_post_context",
            buffer_id="buf-4",
            window_id="win-1",
            end_ms=133,
        ),
        _window("win-1", ["buf-3", "buf-4"], anchor_ms=300, last_frame_index=4),
    ]
    path = _ledger(tmp_path, records)
    listed = [
        media_pb2.EvidenceWindow(
            window_id="win-1",
            trigger="content_change",
            anchor_ms=300,
            frame_buffer_ids=["buf-3", "buf-4"],
            context_before=0,
            context_after=1,
            handed_off_frames=2,
        )
    ]
    report = _coverage_report(4, listed, 1)

    plan = _evidence_plan(report, path)

    assert plan.characterized_frames == 4
    assert plan.anchors == (
        _InputGroup(label="buf-1", trigger="baseline_refresh", frame_ids=("buf-1",)),
        _InputGroup(label="buf-3", trigger="anchor", frame_ids=("buf-3",)),
    )
    assert plan.refused == ()
    # VLM：窗口整窗消费，另外补上没有窗口的基线锚点。
    groups, refused, discards = plan.units(has_vlm=True)
    assert [group.frame_ids for group in groups] == [("buf-3", "buf-4"), ("buf-1",)]
    assert groups[0].label == "win-1"
    assert groups[0].anchor_id == "buf-3"
    assert (refused, discards) == ([], [])
    # OCR：只送锚点；窗口里的上下文帧必须归还给数据面。
    groups, refused, discards = plan.units(has_vlm=False)
    assert groups == [
        _InputGroup(label="buf-1", trigger="anchor", frame_ids=("buf-1",)),
        _InputGroup(label="buf-3", trigger="anchor", frame_ids=("buf-3",)),
    ]
    assert (refused, discards) == ([], ["buf-4"])
    assert TaskExecutor._video_groups(plan, has_vlm=False) == groups


def test_evidence_plan_records_a_retention_rejected_window_instead_of_failing(tmp_path):
    # 锚点帧自己没进数据面，剩下的上下文帧拼不出这个变化：整窗记为被拒单位，
    # 已经交接的上下文帧登记为待归还，整条 Task 不因此失败。
    records = [
        _frame(1, "baseline_anchor", buffer_id="buf-1", decision="first_frame", end_ms=33),
        _frame(
            2,
            "event_anchor",
            decision="content_change",
            window_id="win-1",
            skip_reason=RETENTION_REJECTED,
            start_ms=1000,
            end_ms=1033,
        ),
        _frame(
            3,
            "event_post_context",
            buffer_id="buf-3",
            window_id="win-1",
            start_ms=1033,
            end_ms=1066,
        ),
        _window("win-1", ["buf-3"], anchor_ms=1000, last_frame_index=3),
    ]
    path = _ledger(tmp_path, records)
    report = _coverage_report(3, [], 1)

    plan = _evidence_plan(report, path)

    assert plan.refused == (
        _RejectedUnit(
            kind="anchor",
            label="anchor-00000002",
            trigger="anchor",
            frames=(records[1],),
        ),
        _RejectedUnit(
            kind="window",
            label="win-1",
            trigger="content_change",
            frames=(records[1], records[2]),
            orphan_ids=("buf-3",),
        ),
    )
    assert plan.window_records == 1
    # VLM：事件锚点被拒时它所在的窗口同时被拒，不重复登记；上下文帧必须归还。
    groups, refused, discards = plan.units(has_vlm=True)
    assert [group.frame_ids for group in groups] == [("buf-1",)]
    assert [unit.label for unit in refused] == ["win-1"]
    assert discards == ["buf-3"]
    # OCR：没有可送的锚点，被拒的就是那个锚点单位。
    groups, refused, discards = plan.units(has_vlm=False)
    assert [group.frame_ids for group in groups] == [("buf-1",)]
    assert [unit.label for unit in refused] == ["anchor-00000002"]
    assert discards == ["buf-3"]


def test_modality_view_returns_rejected_frames_as_not_observed_records():
    refused = _RejectedUnit(
        kind="anchor",
        label="anchor-00000007",
        trigger="baseline_refresh",
        frames=(
            _frame(
                7,
                "baseline_anchor",
                skip_reason=RETENTION_REJECTED,
                start_ms=7000,
                end_ms=7033,
            ),
        ),
    )

    groups, units, _discards = _modality_view([], [], [refused], has_vlm=False)

    assert groups == []
    assert [unit.label for unit in units] == ["anchor-00000007"]


def test_refused_frames_never_look_like_a_runtime_descriptor():
    """被拒帧不能凑出「buffer_id + 内容摘要 + 窗口」三元组。

    Timeline 的账本构建把三者同时在场读成"Runtime 真签发过这条描述符"，空摘要会被判成
    `worker_report_invalid_digest`，于是"诚实降级"又变回整条 Task 硬失败。被拒帧从未拿到
    描述符，因此只能留下时间范围（供覆盖层标注）与稳定原因码。
    """
    refused = _RejectedUnit(
        kind="window",
        label="win-1",
        trigger="content_change",
        frames=(
            _frame(
                2,
                "event_anchor",
                skip_reason=RETENTION_REJECTED,
                start_ms=1000,
                end_ms=1033,
            ),
            _frame(
                3,
                "event_post_context",
                buffer_id="buf-3",
                window_id="win-1",
                start_ms=1033,
                end_ms=1066,
            ),
        ),
        orphan_ids=("buf-3",),
    )

    records = _refused_records(refused)

    assert [record["source_time_range_ms"] for record in records] == [[1000, 1033], [1033, 1066]]
    for record in records:
        assert record["error"] == {"reason": RETENTION_REJECTED, "retryable": False}
        assert record["buffer_id"] is None
        assert record["source_digest"] is None
        assert not all(
            record[key] is not None
            for key in ("buffer_id", "source_digest", "source_time_range_ms")
        )


def test_evidence_stream_consumes_incrementally_and_reconciles_with_the_full_plan(tmp_path):
    records = [
        _frame(1, "baseline_anchor", buffer_id="buf-1", decision="first_frame", end_ms=33),
        _frame(
            2,
            "event_anchor",
            buffer_id="buf-2",
            decision="content_change",
            window_id="win-1",
            end_ms=66,
        ),
        _frame(3, "event_post_context", buffer_id="buf-3", window_id="win-1", end_ms=100),
        _window("win-1", ["buf-2", "buf-3"], anchor_ms=200, last_frame_index=3),
    ]
    path = _ledger(tmp_path, records[:2])
    stream = _EvidenceStream(path, has_vlm=True)

    # 解码还在进行：基线锚点先落账，窗口还没到。
    groups, refused, discards = stream.poll()
    assert [group.frame_ids for group in groups] == [("buf-1",)]
    assert (refused, discards) == ([], [])
    # 窗口的帧全部落账之后窗口才出现，消费者这时才领料。
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(records[2], sort_keys=True) + "\n")
        handle.write(json.dumps(records[3], sort_keys=True) + "\n")
    groups, refused, discards = stream.poll()
    assert [group.frame_ids for group in groups] == [("buf-2", "buf-3")]
    assert (refused, discards) == ([], [])

    report = _coverage_report(3, [], 1)
    plan = _evidence_plan(report, path)
    TaskExecutor._reconcile_plan(plan, stream, has_vlm=True)


def test_evidence_stream_detects_a_plan_it_did_not_consume(tmp_path):
    records = [
        _frame(1, "baseline_anchor", buffer_id="buf-1", decision="first_frame", end_ms=33),
        _frame(2, "not_selected", skip_reason="no_change_yet", end_ms=66),
    ]
    path = _ledger(tmp_path, records)
    stream = _EvidenceStream(path, has_vlm=False)
    stream.poll()
    report = _coverage_report(2, [], 0)

    # 完整账本里没有更多单位，流式消费也对齐；把流里少跑一个单位改出来必须被发现。
    plan = _evidence_plan(report, path)
    stream.groups.clear()
    with pytest.raises(TaskExecutionError, match="evidence_stream_plan_mismatch"):
        TaskExecutor._reconcile_plan(plan, stream, has_vlm=False)


def test_evidence_plan_refuses_an_incomplete_ledger(tmp_path):
    records = [_frame(1, "baseline_anchor", buffer_id="buf-1", decision="first_frame", end_ms=33)]
    path = _ledger(tmp_path, records)
    report = _coverage_report(2, [], 0)

    with pytest.raises(TaskExecutionError, match="evidence_ledger_incomplete"):
        _evidence_plan(report, path)


def test_evidence_plan_requires_the_runtime_to_report_coverage(tmp_path):
    with pytest.raises(TaskExecutionError, match="evidence_plan_missing"):
        _evidence_plan(media_pb2.ReplayReport(), tmp_path / "missing.jsonl")


def test_evidence_policy_is_bounded_and_rejects_out_of_range_values():
    bounded = _bounded_policy(policy(evidence_max_gap_ms=1000, evidence_context_before=4))
    assert bounded["evidence_max_gap_ms"] == 1000
    assert bounded["evidence_context_before"] == 4

    for overrides in (
        {"evidence_max_gap_ms": 50},
        {"evidence_max_gap_ms": 120_000},
        {"evidence_context_before": 5},
        {"evidence_change_threshold": 0},
        {"evidence_text_change_threshold": 0},
        {"evidence_min_event_interval_ms": 10},
        {"evidence_retention_bytes": 1024},
    ):
        with pytest.raises(TaskExecutionError, match="task_execution_policy_invalid"):
            _bounded_policy(policy(**overrides))


def test_executor_rejects_audio_overlap_equal_to_a_segment():
    with pytest.raises(TaskExecutionError, match="task_execution_policy_invalid"):
        _bounded_policy(policy(audio_overlap_ms=6000))


class _Plane:
    """数据面消费者的最小替身：只实现 `List` 与"领取后立刻归还"。"""

    def __init__(self, entries: list[SimpleNamespace]):
        self._entries = list(entries)
        self.discarded: list[str] = []

    def listing(self):
        return SimpleNamespace(buffers=list(self._entries))

    def discard(self, buffer_id: str):
        self.discarded.append(buffer_id)
        self._entries = [entry for entry in self._entries if entry.buffer_id != buffer_id]
        return SimpleNamespace()


def _entry(buffer_id: str, kind: str) -> SimpleNamespace:
    return SimpleNamespace(buffer_id=buffer_id, kind=kind)


def test_replay_report_ready_only_accepts_a_complete_report(tmp_path):
    """报告是并发消费者判断"账本已经完整"的唯一凭据，半截文件绝不能被读成完整账本。"""
    path = tmp_path / "local-host.replay.pb"
    assert TaskExecutor._replay_report_ready(path) is None, "还没落盘"

    # 就地覆写会让读到半个文件的消费者误判，因此 Runtime 写的是"临时文件 + 改名"：
    # 这里把中间态也断言掉，改名前后都不能冒出"可读但残缺"的报告。
    path.write_bytes(b"")
    assert TaskExecutor._replay_report_ready(path) is None, "刚创建、还没有内容"

    complete = media_pb2.ReplayReport(platform="macos").SerializeToString()
    path.write_bytes(complete + b"\x0a\x7f")
    assert TaskExecutor._replay_report_ready(path) is None, "多出半截字段"

    path.write_bytes(complete)
    report = TaskExecutor._replay_report_ready(path)
    assert report is not None and report.platform == "macos"


def test_drain_plane_returns_everything_the_plugin_did_not_consume(tmp_path):
    """保留表以**当前保留项**为准：账本没记录过的 kind 同样要有归宿。"""
    plane = _Plane(
        [
            _entry("b-anchor", "video_frame"),
            _entry("b-pre", "video_frame"),
            _entry("b-pcm", "audio_pcm"),
        ]
    )
    returned = TaskExecutor(None, base_dir=tmp_path)._drain_plane(plane, {"b-anchor"})

    assert set(returned) == {"b-pre", "b-pcm"}
    assert plane.discarded == ["b-pre", "b-pcm"]


def test_audio_node_returns_the_kinds_it_does_not_consume(tmp_path):
    """音频节点只消费 `audio_segment`：视频帧与音频 PCM 当场归还，段才不会被挤成拒绝。"""
    plane = _Plane(
        [
            _entry("b-seg", "audio_segment"),
            _entry("b-frame", "video_frame"),
            _entry("b-pcm", "audio_pcm"),
        ]
    )
    returned = TaskExecutor._release_foreign(plane, "audio_segment")

    assert returned == ["b-frame", "b-pcm"]
    assert plane.discarded == ["b-frame", "b-pcm"]
