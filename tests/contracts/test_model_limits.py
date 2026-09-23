"""M8 剩余（ADR-021）：模型 worker 的分级并发上限——解析、准入、有界并发与重试语义。

这些用例不启动真实模型：分级解析/准入/账目都是纯判定，插件侧用**契约形状的替身**
（真 `ProcessRequest`/`ProcessResponse` 消息，去掉 gRPC）。真实插件与真实运行时的验收在
`tools/verify_model_parallelism.py`（`make parallelism-check`）。

用例里的数字必须能和 `crates/runtime/src/lib.rs` 的 `ResidentLimits` 对上：同一批注入值
在 Rust 侧与 Python 侧不能有两套语义。
"""

import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from edge_material_sdk.generated.runtime.v1 import runtime_pb2  # noqa: E402

from tools import ai_worker, model_limits  # noqa: E402


def residency(tier: str = "large", capacity: int = 3) -> runtime_pb2.ResidencyLimits:
    return runtime_pb2.ResidencyLimits(
        tier=tier, media_queue_capacity=64, model_parallelism=capacity
    )


def observation(observation_id: str = "obs_1") -> material.Observation:
    return material.Observation(
        observation_id=observation_id,
        modality="text_embedding",
        stream_id="stream_1",
        source_id="source_1",
        content_hash="sha256:" + "0" * 64,
        time_range=common.TimeRange(start_ms=0, end_ms=10),
    )


def ok(*ids: str) -> runtime_pb2.ProcessResponse:
    return runtime_pb2.ProcessResponse(
        observations=[observation(item) for item in (ids or ("obs_1",))]
    )


def rejection(
    code: int = common.RESOURCE_EXHAUSTED, reason: str = "concurrency_limit", retryable: bool = True
) -> runtime_pb2.ProcessResponse:
    return runtime_pb2.ProcessResponse(
        error=common.ProcessingError(code=code, reason_code=reason, retryable=retryable)
    )


def make_job(index: int = 0, deadline_ms: int = ai_worker.DEFAULT_DEADLINE_MS) -> ai_worker.Job:
    request = runtime_pb2.ProcessRequest(
        context=common.RequestContext(
            request_id=f"req_{index}",
            trace_id="trace_1",
            pipeline_run_id="run_1",
            stream_id="stream_1",
            source_id="source_1",
            deadline_unix_ms=int(time.time() * 1000) + deadline_ms,
            attempt=1,
            idempotency_key=f"req_{index}",
            privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
        ),
        processor_release_id="0.1.0",
    )
    return ai_worker.Job(
        request=request,
        frame={"input": index},
        source_digest="sha256:" + "1" * 64,
        source_range_ms=(0, 10),
    )


class ScriptedPlugin:
    """按脚本返回响应（用尽后重复最后一条），并记录每次调用的 deadline 与在飞并发。"""

    def __init__(self, script: list, *, delay_s: float = 0.0) -> None:
        self.script = list(script)
        self.delay_s = delay_s
        self.deadlines: list[int] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = threading.Lock()

    def Process(self, request, timeout=None):  # noqa: N802 - 契约方法名
        with self._lock:
            index = len(self.deadlines)
            self.deadlines.append(request.context.deadline_unix_ms)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            return self.script[min(index, len(self.script) - 1)]
        finally:
            with self._lock:
                self.in_flight -= 1


def ledger(limit: int = 1, max_attempts: int = ai_worker.DEFAULT_MAX_ATTEMPTS):
    return model_limits.InFlightLedger(limit=limit, max_attempts=max_attempts)


def empty_report() -> dict:
    return {"observations": [], "frames": []}


# ── 解析：与 Rust `parse_resident_limit` 同一套三分法 ────────────────────────


def test_missing_value_is_not_injected_not_a_default():
    assert model_limits.parse_limit(None) is None
    assert model_limits.admit(env_value=None) == model_limits.Admission(
        state="not_injected", limit=1, requested=None, source="none"
    )


@pytest.mark.parametrize("raw", ["", "   ", "0", "-1", "abc", "3.0", "３", "0x3"])
def test_bad_values_are_config_errors(raw):
    with pytest.raises(model_limits.ResidentLimitError) as failure:
        model_limits.parse_limit(raw)
    assert str(failure.value).startswith("invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM")


@pytest.mark.parametrize("raw", ["3", " 3 ", "+3"])
def test_whitespace_and_plus_sign_are_accepted(raw):
    """前导 `+` 与空白在两边的解析器里都是同一个值，不能一边认一边不认。"""
    assert model_limits.parse_limit(raw) == 3


def test_flag_source_is_named_in_the_error():
    with pytest.raises(model_limits.ResidentLimitError) as failure:
        model_limits.parse_limit("abc", source=model_limits.MODEL_PARALLELISM_FLAG)
    assert str(failure.value) == "invalid_resident_limit: --model-parallelism=abc"


# ── 准入：请求值（flag > env）与运行时上限 ───────────────────────────────────


def test_env_only_is_admitted_with_env_as_source():
    admitted = model_limits.admit(env_value="3")
    assert (admitted.state, admitted.limit, admitted.source) == ("admitted", 3, "env")
    assert admitted.as_report()["tier"] == "not_checked"
    assert admitted.as_report()["tier_capacity"] is None


def test_flag_only_is_admitted_with_flag_as_source():
    admitted = model_limits.admit(env_value=None, flag_value="2")
    assert (admitted.state, admitted.limit, admitted.source) == ("admitted", 2, "flag")


def test_conflicting_sources_are_rejected_instead_of_guessed():
    with pytest.raises(model_limits.ResidentLimitError) as failure:
        model_limits.admit(env_value="3", flag_value="2")
    assert str(failure.value) == "model_parallelism_conflict: env=3 flag=2"


def test_equal_sources_are_admitted():
    admitted = model_limits.admit(env_value="3", flag_value="3")
    assert (admitted.limit, admitted.source) == (3, "flag")


def test_runtime_cap_is_adopted_when_nothing_is_requested():
    tier = model_limits.tier_from_residency(residency("large", 3))
    admitted = model_limits.admit(env_value=None, tier=tier)
    assert (admitted.state, admitted.limit, admitted.source) == ("admitted", 3, "runtime")
    assert admitted.as_report()["tier_capacity"] == 3


def test_request_above_tier_cap_is_rejected_and_equal_is_admitted():
    tier = model_limits.tier_from_residency(residency("large", 3))
    assert model_limits.admit(env_value="3", tier=tier).limit == 3
    with pytest.raises(model_limits.ResidentLimitError) as failure:
        model_limits.admit(env_value="8", tier=tier)
    assert str(failure.value) == (
        "model_parallelism_exceeds_tier_cap: requested=8 tier_capacity=3 tier=large"
    )


def test_residency_zero_or_empty_means_not_declared():
    """契约里 `model_parallelism=0` 与空档位名都是"未注入"，不是"无上限"也不是某一档。"""
    tier = model_limits.tier_from_residency(residency("", 0))
    assert tier == model_limits.TierCap(tier="", capacity=None)
    assert model_limits.admit(env_value=None, tier=tier).state == "not_injected"
    # 运行时没声明上限时，请求值只要自身合法就被准入（此时没有可比的上限）。
    assert model_limits.admit(env_value="4", tier=tier).limit == 4


# ── 重试策略与账目 ──────────────────────────────────────────────────────────


def test_retry_policy_is_bounded():
    assert model_limits.should_retry(retryable=True, attempt=1, max_attempts=3)
    assert not model_limits.should_retry(retryable=True, attempt=3, max_attempts=3)
    assert not model_limits.should_retry(retryable=False, attempt=1, max_attempts=3)


def test_retry_delay_grows_and_is_capped():
    assert model_limits.retry_delay_ms(0, base_ms=0) == 0
    assert model_limits.retry_delay_ms(1, base_ms=100) == 100
    assert model_limits.retry_delay_ms(2, base_ms=100) == 200
    assert model_limits.retry_delay_ms(9, base_ms=100) == 1000


def test_ledger_peak_is_measured_across_threads():
    book = ledger(limit=4)
    barrier = threading.Barrier(4)

    def worker():
        with book.call():
            barrier.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert book.peak_in_flight == 4
    assert book.attempts == 4
    assert book.as_report(submitted=4)["peak_in_flight"] == 4


def test_ledger_report_shape_is_stable():
    book = ledger(limit=2)
    book.record_retry("concurrency_limit")
    book.record_retry("concurrency_limit")
    book.record_completed()
    book.record_failed()
    book.record_exhausted()
    report = book.as_report(submitted=3)
    assert report == {
        "submitted": 3,
        "attempts": 0,
        "retries": 2,
        "completed": 1,
        "failed": 2,
        "exhausted": 1,
        "peak_in_flight": 0,
        "max_attempts": ai_worker.DEFAULT_MAX_ATTEMPTS,
        "throttle_events": {"concurrency_limit": 2},
    }


# ── worker 侧：重试不再是终态、有界并发、报告顺序 ────────────────────────────


def test_retryable_rejection_is_retried_then_succeeds():
    plugin = ScriptedPlugin([rejection(), ok("obs_1")])
    book = ledger()
    entries, observations = ai_worker.process_one(
        plugin, make_job(), ledger=book, timeout_s=5.0, deadline_ms=1000, base_backoff_ms=0
    )
    assert [entry["attempts"] for entry in entries] == [2]
    # 观测按 `MessageToDict` 落进报告，键名是 proto 的 camelCase（与既有验收脚本一致）。
    assert [item["observationId"] for item in observations] == ["obs_1"]
    assert (book.retries, book.completed, book.failed, book.exhausted) == (1, 1, 0, 0)
    assert dict(book.throttle_events) == {"concurrency_limit": 1}


def test_every_attempt_refreshes_the_deadline():
    """已经过期的 deadline 不能被原样重试：那样第 2 次只会再拿一个 deadline_expired。"""
    job = make_job(deadline_ms=-60_000)
    plugin = ScriptedPlugin([rejection(), ok()])
    started = int(time.time() * 1000)
    ai_worker.process_one(
        plugin, job, ledger=ledger(), timeout_s=5.0, deadline_ms=1_000, base_backoff_ms=0
    )
    assert all(deadline >= started for deadline in plugin.deadlines)


def test_retry_budget_exhaustion_is_recorded_not_swallowed():
    plugin = ScriptedPlugin([rejection()])
    book = ledger(max_attempts=3)
    entries, observations = ai_worker.process_one(
        plugin, make_job(), ledger=book, timeout_s=5.0, deadline_ms=1000, base_backoff_ms=0
    )
    assert observations == []
    assert entries[0]["error"]["reason"] == "retry_exhausted:concurrency_limit"
    assert entries[0]["attempts"] == 3
    assert (book.retries, book.exhausted, book.failed, book.completed) == (2, 1, 1, 0)


def test_non_retryable_rejection_is_not_retried():
    plugin = ScriptedPlugin([rejection(common.INVALID_INPUT, "invalid_batch_size", False)])
    book = ledger()
    entries, _ = ai_worker.process_one(
        plugin, make_job(), ledger=book, timeout_s=5.0, deadline_ms=1000, base_backoff_ms=0
    )
    assert entries[0]["error"] == {
        "code": "INVALID_INPUT",
        "reason": "invalid_batch_size",
        "retryable": False,
        "attempts": 1,
    }
    assert (book.retries, book.failed) == (0, 1)


def test_success_without_observation_is_a_contract_violation():
    """没有 error 也没有观测：不能让这条输入在账目里既不算成功也不算失败。"""
    plugin = ScriptedPlugin([runtime_pb2.ProcessResponse()])
    book = ledger()
    entries, observations = ai_worker.process_one(
        plugin, make_job(), ledger=book, timeout_s=5.0, deadline_ms=1000, base_backoff_ms=0
    )
    assert observations == []
    assert entries[0]["error"]["reason"] == "empty_plugin_result"
    assert (book.failed, book.completed) == (1, 0)


def test_in_flight_never_exceeds_the_admitted_limit():
    jobs = [make_job(index) for index in range(6)]
    plugin = ScriptedPlugin([ok()], delay_s=0.05)
    book = ledger(limit=3)
    report = empty_report()
    assert (
        ai_worker.process_inputs(plugin, jobs, report, 10.0, ledger=book, base_backoff_ms=0) is None
    )
    assert plugin.peak_in_flight == 3
    assert book.peak_in_flight == 3
    assert len(report["observations"]) == 6


def test_limit_one_keeps_the_previous_sequential_behaviour():
    jobs = [make_job(index) for index in range(3)]
    plugin = ScriptedPlugin([ok()], delay_s=0.02)
    book = ledger(limit=1)
    report = empty_report()
    ai_worker.process_inputs(plugin, jobs, report, 10.0, ledger=book, base_backoff_ms=0)
    assert plugin.peak_in_flight == 1
    assert book.throttle_events == {}


def test_report_keeps_input_order_under_concurrency():
    jobs = [make_job(index) for index in range(5)]
    plugin = ScriptedPlugin([ok()], delay_s=0.01)
    report = empty_report()
    ai_worker.process_inputs(plugin, jobs, report, 10.0, ledger=ledger(limit=5), base_backoff_ms=0)
    assert [frame["input"] for frame in report["frames"]] == [0, 1, 2, 3, 4]
