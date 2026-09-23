"""模型 worker 的分级并发上限：解析、准入与在飞并发账目（ADR-021）。

ADR-015 把"模型 worker 的并发预算"写进统一内存分级表，ADR-019 让**运行时**消费了
`queue_capacity`（越界即失败），但 `SENSORYPLEX_MODEL_PARALLELISM` 一直只有"已声明"这一层：
没有任何执行点读它，也没有并发执行的端到端样本。本模块补上另一半——让**模型 worker**
（`tools/ai_worker.py`）真正按它限流。

判定沿用 Rust 侧 `ResidentLimits` 的三分法（`crates/runtime/src/lib.rs`）：同一批注入值
必须只有一个语义，否则"未注入"与"注入了个 0"在两边会被读成不同的东西。

- **缺失**是事实（开发机上没有 `resident.env` 是常态）：报 `not_injected`，按既有串行语义
  （上限 1）跑——不填"看起来合理"的默认值；
- **坏值**（空串 / 0 / 非数字）是坏配置：`invalid_resident_limit: <来源>`，直接失败，不跑；
- **越界**（请求值 > 运行时转述的分级上限）：`model_parallelism_exceeds_tier_cap: …`，直接失败，
  不夹取、不降级、不改写配置。

上限的**权威是运行时**：`DescribeCapabilities.residency` 已经转述了分级表
（ADR-015 §5 / ADR-019 §5）。
于是 worker 把"这次想要几路"与"这一档允许几路"分开：前者来自 `--model-parallelism` 或
`SENSORYPLEX_MODEL_PARALLELISM`，后者来自运行时；报告里的 `source` 写明最后采用的是哪一个，
不做"两个来源谁大用谁"这类推断。

单位是**并发在飞的插件调用数**。分级表的措辞是"N 个 worker"，本切片把它落成 worker 侧的
在飞调用上限：`limit == 1` 与本模块之前的串行语义完全一致，因此默认行为不变。
"""

import re
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field

MODEL_PARALLELISM_ENV = "SENSORYPLEX_MODEL_PARALLELISM"
MODEL_PARALLELISM_FLAG = "--model-parallelism"

# 与 Rust 的 `str::parse::<usize>()` 对齐：接受前导 `+`，不接受小数、Unicode 数字或空串。
# 不用裸 `int()`：它会接受 `"３"`（全角数字）这类 Rust 侧不认的输入，两边语义会分叉。
_POSITIVE_INTEGER = re.compile(r"^\+?[0-9]+$")


class ResidentLimitError(ValueError):
    """稳定原因串：`str(error)` 就是报告与日志里的失败原因。"""


def parse_limit(raw: str | None, *, source: str = MODEL_PARALLELISM_ENV) -> int | None:
    """`None` 表示**未注入**；空串、非整数与非正值都是坏配置。"""
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        raise ResidentLimitError(f"invalid_resident_limit: {source} is set but empty")
    if not _POSITIVE_INTEGER.match(value):
        raise ResidentLimitError(f"invalid_resident_limit: {source}={value}")
    parsed = int(value)
    if parsed <= 0:
        raise ResidentLimitError(f"invalid_resident_limit: {source}={value}")
    return parsed


@dataclass(frozen=True)
class TierCap:
    """运行时转述的分级上限。

    `capacity=None` 表示运行时**没有声明**这一项（契约里用 0 表示"未注入"），不是"无上限"。
    """

    tier: str = ""
    capacity: int | None = None


def tier_from_residency(residency) -> TierCap:
    """把 `DescribeCapabilities.residency` 变成准入输入：0 与空串都读成"未声明"。"""
    tier = getattr(residency, "tier", "") or ""
    raw = int(getattr(residency, "model_parallelism", 0) or 0)
    return TierCap(tier=str(tier), capacity=raw if raw > 0 else None)


@dataclass(frozen=True)
class Admission:
    """一次准入的结果；`state` 只有 `not_injected` 与 `admitted` 两种（越界在之前就失败）。"""

    state: str
    limit: int
    requested: int | None
    source: str
    tier: str = ""
    tier_capacity: int | None = None

    def as_report(self) -> dict:
        return {
            "state": self.state,
            "limit": self.limit,
            "requested": self.requested,
            "source": self.source,
            # 没接运行时就是"没核对"，不写成一个看起来像档位的字符串。
            "tier": self.tier or "not_checked",
            # `null` 而不是 0：0 在契约里已经被"未声明"占用，报告里不该再借它表示未知。
            "tier_capacity": self.tier_capacity,
        }


def admit(
    *,
    env_value: str | None,
    flag_value: str | None = None,
    tier: TierCap | None = None,
) -> Admission:
    """请求值（flag 优先于 env）与运行时上限的三方准入。"""
    env_limit = parse_limit(env_value)
    flag_limit = parse_limit(flag_value, source=MODEL_PARALLELISM_FLAG)
    if env_limit is not None and flag_limit is not None and env_limit != flag_limit:
        # 两个来源都在却不一样：静默选一个等于把冲突藏起来，直接拒绝。
        raise ResidentLimitError(f"model_parallelism_conflict: env={env_limit} flag={flag_limit}")
    requested = flag_limit if flag_limit is not None else env_limit
    capacity = tier.capacity if tier is not None else None
    name = (tier.tier if tier is not None else "") or ""
    if requested is not None and capacity is not None and requested > capacity:
        raise ResidentLimitError(
            "model_parallelism_exceeds_tier_cap: "
            f"requested={requested} tier_capacity={capacity} tier={name or 'unnamed'}"
        )
    if requested is not None:
        return Admission(
            state="admitted",
            limit=requested,
            requested=requested,
            source="flag" if flag_limit is not None else "env",
            tier=name,
            tier_capacity=capacity,
        )
    if capacity is not None:
        # 没有请求值、但运行时说了这一档允许几路：采用它，并在报告里写明来源是 runtime。
        return Admission(
            state="admitted",
            limit=capacity,
            requested=None,
            source="runtime",
            tier=name,
            tier_capacity=capacity,
        )
    return Admission(state="not_injected", limit=1, requested=None, source="none")


def should_retry(*, retryable: bool, attempt: int, max_attempts: int) -> bool:
    """只有插件自己声明可重试的拒绝才有下一次；次数由 `max_attempts` 封顶。"""
    return retryable and attempt < max_attempts


def retry_delay_ms(attempt: int, *, base_ms: int) -> int:
    """指数退避并封顶 1s。`attempt` 从 1 开始，表示"第 attempt 次调用失败后等多久"。"""
    if base_ms <= 0:
        return 0
    return min(base_ms * (2 ** (attempt - 1)), 1000)


@dataclass
class InFlightLedger:
    """在飞并发、重试与最终归宿的账目；报告里的数字都来自这里，不在调用点各记一份。"""

    limit: int
    max_attempts: int
    _in_flight: int = 0
    attempts: int = 0
    retries: int = 0
    completed: int = 0
    failed: int = 0
    exhausted: int = 0
    peak_in_flight: int = 0
    throttle_events: Counter = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @contextmanager
    def call(self):
        """一次插件调用（含重试）的在飞窗口。"""
        with self._lock:
            self.attempts += 1
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def record_retry(self, reason_code: str) -> None:
        with self._lock:
            self.retries += 1
            # 按原因码分开记：`concurrency_limit` 与 `deadline_expired` 说明的东西不一样。
            self.throttle_events[reason_code] += 1

    def record_completed(self) -> None:
        with self._lock:
            self.completed += 1

    def record_failed(self) -> None:
        with self._lock:
            self.failed += 1

    def record_exhausted(self) -> None:
        with self._lock:
            self.failed += 1
            self.exhausted += 1

    def as_report(self, *, submitted: int) -> dict:
        with self._lock:
            return {
                "submitted": submitted,
                "attempts": self.attempts,
                "retries": self.retries,
                "completed": self.completed,
                "failed": self.failed,
                "exhausted": self.exhausted,
                "peak_in_flight": self.peak_in_flight,
                "max_attempts": self.max_attempts,
                "throttle_events": dict(self.throttle_events),
            }
