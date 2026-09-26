"""受控 v2 执行器的纯契约：不启动媒体或模型进程。"""

from types import SimpleNamespace

import pytest

from tools.task_executor import TaskExecutionError, TaskExecutor, _bounded_policy, _runtime_pipeline


def test_runtime_pipeline_does_not_declare_vlm_when_revision_has_no_vlm():
    policy = {
        "window_ms": 1000,
        "sample_interval_ms": 1000,
        "audio_segment_ms": 6000,
        "audio_overlap_ms": 500,
        "vlm_sample_interval_ms": 5000,
    }

    without_vlm = _runtime_pipeline(policy, has_vlm=False)
    with_vlm = _runtime_pipeline(policy, has_vlm=True)

    assert "slow_enrichment: []" in without_vlm
    assert "- type: vlm" not in without_vlm
    assert "enrichment_modalities: []" in without_vlm
    assert "- type: vlm" in with_vlm
    assert "- vision.scene_description" in with_vlm


def test_empty_observations_still_produce_complete_one_second_coverage(tmp_path):
    source = SimpleNamespace(
        source=SimpleNamespace(
            duration_ms=9056,
            tracks=[SimpleNamespace(track_kind="video")],
        )
    )
    policy = {
        "window_ms": 1000,
        "sample_interval_ms": 1000,
        "audio_segment_ms": 6000,
        "audio_overlap_ms": 500,
        "vlm_sample_interval_ms": 5000,
    }

    coverage = TaskExecutor(None, base_dir=tmp_path)._coverage(source, [], {}, policy)

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


def test_executor_rejects_audio_overlap_equal_to_a_segment():
    with pytest.raises(TaskExecutionError, match="task_execution_policy_invalid"):
        _bounded_policy(
            {
                "window_ms": 1000,
                "sample_interval_ms": 1000,
                "audio_segment_ms": 6000,
                "audio_overlap_ms": 6000,
                "vlm_sample_interval_ms": 5000,
            }
        )
