"""NATS JetStream 事件契约的**唯一来源**（ADR-024 发布侧 / ADR-025 消费侧共用）。

发布端（`sensoryplex-relay`）与消费端（`sensoryplex-index-worker`）必须对同一件事达成一致：
stream 名与保留策略、subject 规则、`Nats-Msg-Id` 去重口径、envelope 与 outbox 行的对账。
把这些常量复制成两份，就等于承认"两条会各自漂移的事实源"——所以放这里一处定义，两端都取它。

本模块是**纯契约**：常量与纯函数，不连 NATS、不建 stream、不读 outbox，也不在导入期
引入 `nats-py`（库只在真正连接时才需要）。唯一例外是两个 `async` 帮助函数
（`ensure_stream` / `require_stream`），它们仍然只按契约办事：

- `ensure_stream`（发布端）：不存在就按有界契约建；已存在则**只校验**，漂移即报错；
- `require_stream`（消费端）：**只校验，绝不创建**。消费端凭空建一个流会把"发布端还没
  被部署"伪装成"链路已经通了"，那是一类最容易骗过验收的错误。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from google.protobuf.message import DecodeError

# 事件类型的字符集必须**显式**校验：subject 里出现 `*`/`>`/空格就不是"换了个名字"，
# 而是把一条事件投成了通配订阅或非法 subject。
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39}){0,3}$")
MAX_EVENT_TYPE_LENGTH = 160

DEFAULT_STREAM = "sensoryplex-events"
DEFAULT_SUBJECT_PREFIX = "sensoryplex.events"
# 有界：消息数、字节数、保留时间、去重窗口都写死上限，不允许"无限增长"的流。
STREAM_MAX_MSGS = 200_000
STREAM_MAX_BYTES = 256 << 20
# `max_age` / `duplicate_window` 在 nats-py 的 `StreamConfig` 里就是**秒**（见
# `nats/js/api.py` 的 `max_age: Optional[float] = None  # in seconds`）：库自己负责在
# `as_dict()` 里换算成服务端的纳秒 `time.Duration`，`from_response` 再除回来。
# 把纳秒直接写进去会被**再乘一次 1e9**，服务端报 `cannot unmarshal number ... into
# Go struct field StreamConfigRequest.StreamConfig.max_age`——这是真机跑出来的教训。
STREAM_MAX_AGE_S = 7 * 24 * 3600
STREAM_DUPLICATE_WINDOW_S = 2 * 3600


class EventBusError(Exception):
    """带稳定原因码的事件总线契约错误；调用方按 code 分支，不解析 detail 文本。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class EventScope:
    """stream 与 subject 前缀：发布端与消费端共用的一段身份，校验规则也只此一份。"""

    stream: str = DEFAULT_STREAM
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX

    def validate(self) -> None:
        if not self.stream or len(self.stream) > 128:
            raise EventBusError("invalid_stream_name")
        validate_subject_prefix(self.subject_prefix)

    @property
    def subjects(self) -> list[str]:
        return subjects_for(self.subject_prefix)


def validate_subject_prefix(prefix: str) -> None:
    """前缀必须是一段**可订阅的字面前缀**，不能自带通配符或尾点。"""
    if not prefix or len(prefix) > 128:
        raise EventBusError("invalid_subject_prefix")
    if prefix.endswith("."):
        raise EventBusError("invalid_subject_prefix", "trailing_dot")
    if any(token in prefix for token in ("*", ">")):
        raise EventBusError("invalid_subject_prefix", "wildcard_in_prefix")


def subjects_for(prefix: str) -> list[str]:
    validate_subject_prefix(prefix)
    return [f"{prefix}.>"]


def subject_for(event_type: str, prefix: str) -> str:
    """事件类型 → subject。非法类型**拒绝发布**，不是换个写法硬发。"""
    if (
        not isinstance(event_type, str)
        or not event_type
        or len(event_type) > MAX_EVENT_TYPE_LENGTH
        or not EVENT_TYPE_PATTERN.match(event_type)
    ):
        raise EventBusError("invalid_event_type", event_type[:80] if event_type else "unset")
    return f"{prefix}.{event_type}"


def envelope_of(contract_bytes: bytes, *, expected_event_id: str) -> EventEnvelope:
    """反序列化 outbox 里的契约字节，并校验它与行里的 `event_id` 是同一件事。

    `event_outbox` 同时存 `event_id` 列与序列化后的 EventEnvelope：两者不一致说明写入侧
    有缺陷。这种情况**拒绝发布**——发一条"自称是别的 id"的事件，下游会按去重表把它当成
    另一条事件，账目就再也不可对。
    """
    try:
        envelope = EventEnvelope.FromString(contract_bytes)
    except DecodeError as error:
        raise EventBusError("invalid_event_contract", type(error).__name__) from error
    if not envelope.event_id or envelope.event_id != expected_event_id:
        raise EventBusError("event_envelope_mismatch", envelope.event_id[:80] or "empty")
    if not envelope.event_type:
        raise EventBusError("invalid_event_contract", "empty_event_type")
    return envelope


def stream_snapshot(info) -> dict:
    """把 nats-py 的 `StreamInfo` 折成可比较的字典（秒口径，与写入口径一致）。"""
    config = info.config
    return {
        "subjects": list(config.subjects or []),
        "storage": str(config.storage or ""),
        "max_msgs": config.max_msgs,
        "max_bytes": config.max_bytes,
        "max_age": config.max_age,
        "duplicate_window": config.duplicate_window,
    }


def stream_contract_diff(existing: dict, expected: EventScope) -> list[str]:
    """现有 stream 与契约的差异（为空表示一致）。

    单位口径：两侧都是**秒**（nats-py 在读写两端替我们换算服务端的纳秒），所以比较也按秒。
    刻意连 `duplicate_window` 一起比：它决定"崩溃在已发布未提交之间"的那次重发能不能被吸收，
    被改小就会让重发变成真重复——那和 `max_age` 被改小一样属于静默变质，必须当场报出来。
    """
    diff: list[str] = []
    if sorted(existing.get("subjects") or []) != sorted(expected.subjects):
        diff.append(f"subjects={existing.get('subjects')}!= {expected.subjects}")
    if (existing.get("storage") or "").lower() not in ("file", "filestorage"):
        diff.append(f"storage={existing.get('storage')}")
    if int(existing.get("max_msgs") or 0) != STREAM_MAX_MSGS:
        diff.append(f"max_msgs={existing.get('max_msgs')}!= {STREAM_MAX_MSGS}")
    if int(existing.get("max_bytes") or 0) != STREAM_MAX_BYTES:
        diff.append(f"max_bytes={existing.get('max_bytes')}!= {STREAM_MAX_BYTES}")
    if int(existing.get("max_age") or 0) != STREAM_MAX_AGE_S:
        diff.append(f"max_age={existing.get('max_age')}!= {STREAM_MAX_AGE_S}s")
    if int(existing.get("duplicate_window") or 0) != STREAM_DUPLICATE_WINDOW_S:
        diff.append(
            f"duplicate_window={existing.get('duplicate_window')}!= {STREAM_DUPLICATE_WINDOW_S}s"
        )
    return diff


def stream_config(scope: EventScope):
    """按有界契约构造 `StreamConfig`（只在发布端建流时用）。"""
    import nats.js.api as jsapi

    return jsapi.StreamConfig(
        name=scope.stream,
        subjects=scope.subjects,
        storage=jsapi.StorageType.FILE,
        max_msgs=STREAM_MAX_MSGS,
        max_bytes=STREAM_MAX_BYTES,
        max_age=STREAM_MAX_AGE_S,
        duplicate_window=STREAM_DUPLICATE_WINDOW_S,
    )


def _is_not_found(error: Exception) -> bool:
    return type(error).__name__ == "NotFoundError"


async def ensure_stream(js, scope: EventScope):
    """建/校验 stream；已存在则**只校验**，不悄悄改成别的保留策略。"""
    scope.validate()
    try:
        info = await js.stream_info(scope.stream)
    except Exception as error:  # noqa: BLE001 - nats-py 用 NotFoundError 报"不存在"
        if not _is_not_found(error):
            raise EventBusError("event_stream_unavailable", type(error).__name__) from error
        return await js.add_stream(config=stream_config(scope))
    diff = stream_contract_diff(stream_snapshot(info), scope)
    if diff:
        raise EventBusError("event_stream_contract_mismatch", ";".join(diff)[:200])
    return info


async def require_stream(js, scope: EventScope):
    """消费端：流必须**已存在且**符合契约；缺失与漂移都是显式失败，绝不自动创建。"""
    scope.validate()
    try:
        info = await js.stream_info(scope.stream)
    except Exception as error:  # noqa: BLE001 - nats-py 用 NotFoundError 报"不存在"
        if _is_not_found(error):
            # 消费端建流会把"发布端还没部署"伪装成"链路通了"，所以这里是失败而不是修复。
            raise EventBusError("event_stream_missing", scope.stream) from error
        raise EventBusError("event_stream_unavailable", type(error).__name__) from error
    diff = stream_contract_diff(stream_snapshot(info), scope)
    if diff:
        raise EventBusError("event_stream_contract_mismatch", ";".join(diff)[:200])
    return info


async def connect_bounded(nats_module, nats_url: str, *, name: str, timeout_s: float):
    """有界启动连接：NATS 起不来时**显式失败**，不进入"重连到天荒地老"的半启动状态。

    nats-py 的 `max_reconnect_attempts=-1` 连**首次**连接也是无限重试
    （`_select_next_server` 只在 `max_reconnect_attempts > 0` 时才放弃服务器），所以只靠库
    参数的写法在"NATS 地址填错/服务没起"时表现为进程静默挂着、一行状态都不打——这是最难
    排查的一类失败，也违反了本仓"失败必须显式"的口径。这里给启动加一个上限，连上之后
    仍然把稳态断线交给库做无限重连：NATS 重启不该让常驻进程退出，而这一跳的失败会落进
    各自的账目（发布端不写 `published_at`，消费端不 `ack`）。
    """
    import asyncio

    try:
        return await asyncio.wait_for(
            nats_module.connect(
                nats_url,
                name=name,
                max_reconnect_attempts=-1,
                reconnect_time_wait=1,
                connect_timeout=timeout_s,
            ),
            timeout=timeout_s * 2,
        )
    except TimeoutError as error:
        raise EventBusError("nats_unreachable", type(error).__name__) from error
    except Exception as error:  # noqa: BLE001 - 连接失败一律按"不可达"上报，类名进 detail
        raise EventBusError("nats_unreachable", type(error).__name__) from error
