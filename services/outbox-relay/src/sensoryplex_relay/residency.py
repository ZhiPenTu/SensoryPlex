"""分级背压准入：把 ADR-019 的"分级是准入上限"接到事件链路（ADR-027）。

媒体作业那一侧的准入在 Rust（`crates/runtime`，读同一个环境变量），这里是对称的 Python 一侧：
relay 每轮的认领深度与消费端"在飞未 ack"的深度都必须落在本档上限之内。越界**拒绝启动**，
不夹取、不改写、不静默降级——夹取会让"配置里写的 200"与"实际在飞 64"长期不一致，而这两
个数恰好是背压的分子。

三种输入、三种结果（与 ADR-019 §3 逐字对齐）：

| 输入 | 结果 |
| --- | --- |
| 上限变量缺失 | `not_injected`：上限记为 0，**绝不**填默认值 |
| 空串 | `invalid_resident_limit: <VAR> is set but empty`：设置了空串 ≠ 没设置 |
| `0` / 非数字 | `invalid_resident_limit: <VAR>=<value>`：调用方在连任何东西之前显式失败 |
| 声明值 > 上限 | `event_inflight_exceeds_tier_cap: declared=<n> tier_capacity=<n> tier=<name>` |

上限值本身由 `tools/macos_resident.py` 的分级表渲染进 `resident.env`（与媒体链路的上限变量同一处），
所以"哪个档位给多少"只有一份事实源；这个模块只负责**读环境并判定**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 与 `crates/runtime` 读的是同一组变量名（ADR-019 §2/§4）：分级是单一事实源。
TIER_VAR = "SENSORYPLEX_RESIDENT_TIER"
CAPACITY_VAR = "SENSORYPLEX_EVENT_QUEUE_CAPACITY"

# 状态行的取值：只有这两个字符串，不发明第三种"看起来在跑"的中间态。
NOT_INJECTED = "not_injected"
ADMITTED = "admitted"


class ResidencyError(Exception):
    """带稳定原因码的准入失败；调用方按码分支，不解析文本。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EventBackpressure:
    """一次准入的结果：状态、档位、声明值与上限。

    `capacity == 0` 只出现在 `not_injected`：那是"没有上限可判"，不是"上限是 0 条"。
    """

    state: str
    tier: str
    declared: int
    capacity: int

    def document(self) -> dict[str, str | int]:
        """给状态行的字段：只有数字、档位名与状态字符串，不含端点与路径。"""

        return {
            "inflight_state": self.state,
            "inflight_declared": self.declared,
            "inflight_capacity": self.capacity,
            "resident_tier": self.tier,
        }


def _parse_limit(name: str, raw: str | None) -> int | None:
    """与 `crates/runtime` 的 `parse_resident_limit` 同一口径，原因码逐字对齐。

    缺失 = `None`（未注入）；空串、非数字、`0` 都是**坏配置**，不是"没有上限"。
    """

    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise ResidencyError(f"invalid_resident_limit: {name} is set but empty")
    try:
        parsed = int(value)
    except ValueError:
        raise ResidencyError(f"invalid_resident_limit: {name}={value}") from None
    if parsed <= 0:
        raise ResidencyError(f"invalid_resident_limit: {name}={value}")
    return parsed


def read_event_backpressure(
    declared: int, *, environ: os._Environ[str] | dict[str, str] | None = None
) -> EventBackpressure:
    """读分级并按 `declared` 做准入；越界/坏值抛 `ResidencyError`（不是返回值）。"""

    env = os.environ if environ is None else environ
    tier = env.get(TIER_VAR)
    if tier is not None:
        # 档位名只用于报告，但"设置了空串"与"根本没设置"是两件事（ADR-019 §3）。
        tier = tier.strip()
        if not tier:
            raise ResidencyError(f"invalid_resident_limit: {TIER_VAR} is set but empty")
    capacity = _parse_limit(CAPACITY_VAR, env.get(CAPACITY_VAR))
    if capacity is None:
        # 开发机没有 resident.env：显式未注入，不猜默认值。
        return EventBackpressure(NOT_INJECTED, tier or NOT_INJECTED, declared, 0)
    if declared > capacity:
        raise ResidencyError(
            f"event_inflight_exceeds_tier_cap: declared={declared}"
            f" tier_capacity={capacity} tier={tier or NOT_INJECTED}"
        )
    return EventBackpressure(ADMITTED, tier or NOT_INJECTED, declared, capacity)
