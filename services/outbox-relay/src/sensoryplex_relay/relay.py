"""outbox → NATS JetStream 的 relay（ADR-024）。

事务性 outbox 的规矩是"事实与事件同事务、发布与提交分开"：relay 只**发布**，不改事实，
也不读载荷内容。四条写死的语义：

1. `published_at` 只在 JetStream **确认收到**之后才写：发布失败时这一列必须保持 NULL，
   否则"压根没发出去"会被后来的进程读成"已经发过"——事件就永久消失了；
2. 发布带 `Nats-Msg-Id = event_id`：崩溃在"已发布、未提交"之间的重发会被 JetStream 的
   duplicate window 吸收，因此**不需要**去猜"到底发没发"；
3. 认领用 `FOR UPDATE SKIP LOCKED` 且**只读**：认领事务立刻提交（不跨网络 I/O 持锁），
   于是同一批可能被两个 relay 实例同时看到——代价是可能重复发布一次，而这个代价由第 2 条
   吸收；反过来"持锁发布"会让一个慢的 NATS 拖住行锁，那才是更贵的错误；
4. 只按 `created_at, event_id` 稳定取，不做重排；发不出去就保留未发布状态并计数，**不丢**。

刻意不做的事：不自动改已存在 stream 的保留策略（那会静默丢掉还没被消费的事件），
不把 `contract_bytes`、载荷、令牌或主机路径写进日志。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass

import psycopg
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
MAX_BATCH = 1000
DEFAULT_BATCH = 200


class RelayError(Exception):
    """带稳定原因码的 relay 失败；调用方按 code 分支，不解析 detail 文本。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class RelayOptions:
    stream: str = DEFAULT_STREAM
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX
    batch: int = DEFAULT_BATCH
    interval_s: float = 1.0
    publish_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    max_batches: int = 0  # 0 = 常驻

    def validate(self) -> None:
        if not self.stream or len(self.stream) > 128:
            raise RelayError("invalid_stream_name")
        if not self.subject_prefix or len(self.subject_prefix) > 128:
            raise RelayError("invalid_subject_prefix")
        if self.subject_prefix.endswith("."):
            raise RelayError("invalid_subject_prefix", "trailing_dot")
        if any(token in self.subject_prefix for token in ("*", ">")):
            raise RelayError("invalid_subject_prefix", "wildcard_in_prefix")
        if not 1 <= self.batch <= MAX_BATCH:
            raise RelayError("invalid_batch", str(self.batch))
        if not 0 < self.interval_s <= 60:
            raise RelayError("invalid_interval", str(self.interval_s))
        if not 0 < self.publish_timeout_s <= 60:
            raise RelayError("invalid_publish_timeout", str(self.publish_timeout_s))
        if not 0 < self.connect_timeout_s <= 120:
            raise RelayError("invalid_connect_timeout", str(self.connect_timeout_s))
        if self.max_batches < 0:
            raise RelayError("invalid_max_batches", str(self.max_batches))

    @property
    def subjects(self) -> list[str]:
        return [f"{self.subject_prefix}.>"]


def subject_for(event_type: str, prefix: str) -> str:
    """事件类型 → subject。非法类型**拒绝发布**，不是换个写法硬发。"""
    if (
        not isinstance(event_type, str)
        or not event_type
        or len(event_type) > MAX_EVENT_TYPE_LENGTH
        or not EVENT_TYPE_PATTERN.match(event_type)
    ):
        raise RelayError("invalid_event_type", event_type[:80] if event_type else "unset")
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
        raise RelayError("invalid_event_contract", type(error).__name__) from error
    if not envelope.event_id or envelope.event_id != expected_event_id:
        raise RelayError("event_envelope_mismatch", envelope.event_id[:80] or "empty")
    if not envelope.event_type:
        raise RelayError("invalid_event_contract", "empty_event_type")
    return envelope


def stream_contract_diff(existing: dict, options: RelayOptions) -> list[str]:
    """现有 stream 与本次契约的差异（为空表示一致）。

    单位口径：两侧都是**秒**（nats-py 在读写两端替我们换算服务端的纳秒），所以比较也按秒。
    刻意连 `duplicate_window` 一起比：它决定"崩溃在已发布未提交之间"的那次重发能不能被吸收，
    被改小就会让重发变成真重复——那和 `max_age` 被改小一样属于静默变质，必须当场报出来。
    """
    diff: list[str] = []
    if sorted(existing.get("subjects") or []) != sorted(options.subjects):
        diff.append(f"subjects={existing.get('subjects')}!= {options.subjects}")
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


async def ensure_stream(js, options: RelayOptions):
    """建/校验 stream；已存在则**只校验**，不悄悄改成别的保留策略。"""
    import nats.js.api as jsapi

    try:
        info = await js.stream_info(options.stream)
    except Exception as error:  # noqa: BLE001 - nats-py 用 NotFoundError 报"不存在"
        if type(error).__name__ != "NotFoundError":
            raise RelayError("event_stream_unavailable", type(error).__name__) from error
        return await js.add_stream(
            config=jsapi.StreamConfig(
                name=options.stream,
                subjects=options.subjects,
                storage=jsapi.StorageType.FILE,
                max_msgs=STREAM_MAX_MSGS,
                max_bytes=STREAM_MAX_BYTES,
                max_age=STREAM_MAX_AGE_S,
                duplicate_window=STREAM_DUPLICATE_WINDOW_S,
            )
        )
    config = info.config
    existing = {
        "subjects": list(config.subjects or []),
        "storage": str(config.storage or ""),
        "max_msgs": config.max_msgs,
        "max_bytes": config.max_bytes,
        "max_age": config.max_age,
        "duplicate_window": config.duplicate_window,
    }
    diff = stream_contract_diff(existing, options)
    if diff:
        raise RelayError("event_stream_contract_mismatch", ";".join(diff)[:200])
    return info


def claim_candidates(conn, batch: int) -> list[dict]:
    """取一批候选（`FOR UPDATE SKIP LOCKED`，只读、立刻提交）。

    这一步**不改变**任何状态：它只是让并发实例尽量不撞同一批。真正的并发保护在
    `mark_published` 的 `published_at IS NULL` 与 JetStream 的 `Nats-Msg-Id` 去重上。
    """
    rows = conn.execute(
        "SELECT event_id, event_type, contract_bytes, attempt FROM event_outbox "
        "WHERE published_at IS NULL ORDER BY created_at, event_id LIMIT %s "
        "FOR UPDATE SKIP LOCKED",
        (batch,),
    ).fetchall()
    conn.commit()
    return [
        {"event_id": row[0], "event_type": row[1], "contract_bytes": row[2], "attempt": row[3]}
        for row in rows
    ]


def mark_published(conn, event_ids: list[str]) -> int:
    """发布成功之后才置 `published_at`；`published_at IS NULL` 是并发保护。"""
    if not event_ids:
        return 0
    rows = conn.execute(
        "UPDATE event_outbox SET published_at=now(), attempt=attempt+1 "
        "WHERE event_id = ANY(%s) AND published_at IS NULL RETURNING event_id",
        (event_ids,),
    ).fetchall()
    conn.commit()
    return len(rows)


def record_failure(conn, event_ids: list[str]) -> int:
    """发布失败：只加尝试计数，**不**碰 `published_at`（那一列只表示"确认发出去了"）。"""
    if not event_ids:
        return 0
    rows = conn.execute(
        "UPDATE event_outbox SET attempt=attempt+1 WHERE event_id = ANY(%s) RETURNING event_id",
        (event_ids,),
    ).fetchall()
    conn.commit()
    return len(rows)


def pending_stats(conn) -> dict:
    row = conn.execute(
        "SELECT count(*), min(created_at) FROM event_outbox WHERE published_at IS NULL"
    ).fetchone()
    pending, oldest = int(row[0]), row[1]
    return {
        "pending": pending,
        "oldest_pending_age_s": round(time.time() - oldest.timestamp(), 1) if oldest else None,
        "max_attempt": int(
            conn.execute("SELECT coalesce(max(attempt), 0) FROM event_outbox").fetchone()[0]
        ),
    }


def status_document(
    *,
    cycle: int,
    options: RelayOptions,
    claimed: int,
    published: int,
    failed: int,
    totals: dict,
    pending: dict,
    error_code: str = "",
    error_detail: str = "",
) -> dict:
    """状态行：只放计数、标识与时间，不放载荷、向量、令牌或主机路径。"""
    return {
        "event": "relay.status",
        "cycle": cycle,
        "stream": options.stream,
        "subject_prefix": options.subject_prefix,
        "claimed": claimed,
        "published": published,
        "failed": failed,
        "published_total": totals["published"],
        "failed_total": totals["failed"],
        "pending": pending["pending"],
        "oldest_pending_age_s": pending["oldest_pending_age_s"],
        "max_attempt": pending["max_attempt"],
        "error_code": error_code,
        # 只放异常**类名**：异常文本可能带地址、DSN 或载荷片段。
        "error_detail": error_detail,
    }


async def publish_event(js, *, subject: str, event_id: str, contract_bytes: bytes, timeout: float):
    """发布单条事件：`Nats-Msg-Id` 就是 outbox 的 `event_id`，重复发布由 JetStream 吸收。"""
    return await js.publish(
        subject, contract_bytes, headers={"Nats-Msg-Id": event_id}, timeout=timeout
    )


async def relay_cycle(conn, js, options: RelayOptions, totals: dict) -> dict:
    """跑一轮：取候选 → 逐条发布 → 记账。单条失败不拖累整批。"""
    candidates = claim_candidates(conn, options.batch)
    published_ids: list[str] = []
    failed_ids: list[str] = []
    error_code = ""
    error_detail = ""
    for entry in candidates:
        try:
            subject = subject_for(entry["event_type"], options.subject_prefix)
            envelope_of(entry["contract_bytes"], expected_event_id=entry["event_id"])
            await publish_event(
                js,
                subject=subject,
                event_id=entry["event_id"],
                contract_bytes=entry["contract_bytes"],
                timeout=options.publish_timeout_s,
            )
        except RelayError as error:
            # 契约问题：发出去也会被下游当成脏数据，宁可留在未发布状态并计数。
            error_code, error_detail = error.code, error.detail
            failed_ids.append(entry["event_id"])
            continue
        except Exception as error:  # noqa: BLE001 - 传输层异常按"可重试"处理
            error_code, error_detail = "event_publish_failed", type(error).__name__
            failed_ids.append(entry["event_id"])
            continue
        published_ids.append(entry["event_id"])
    published = mark_published(conn, published_ids)
    record_failure(conn, failed_ids)
    totals["published"] += published
    totals["failed"] += len(failed_ids)
    return {
        "claimed": len(candidates),
        "published": published,
        "failed": len(failed_ids),
        "error_code": error_code,
        "error_detail": error_detail,
    }


async def run_relay(
    *, database_url: str, nats_url: str, options: RelayOptions, emit, stop=None
) -> int:
    """常驻（或 `max_batches` 轮）循环；返回退出码，失败原因写在状态行里。"""
    import nats

    options.validate()
    totals = {"published": 0, "failed": 0}
    client = await connect_bounded(nats, nats_url, options)
    try:
        js = client.jetstream()
        await ensure_stream(js, options)
        with psycopg.connect(database_url) as conn:
            emit(
                {
                    "event": "relay.ready",
                    "stream": options.stream,
                    "subject_prefix": options.subject_prefix,
                    "batch": options.batch,
                    "interval_s": options.interval_s,
                    "storage": "file",
                    "pending": pending_stats(conn)["pending"],
                }
            )
            cycle = 0
            failures = 0
            while True:
                cycle += 1
                try:
                    outcome = await relay_cycle(conn, js, options, totals)
                    failures = failures + 1 if outcome["failed"] else 0
                except psycopg.Error as error:
                    # 元数据库不可用：不臆造"发过了"，这一轮什么都不记。
                    conn.rollback()
                    outcome = {
                        "claimed": 0,
                        "published": 0,
                        "failed": 0,
                        "error_code": "metadata_store_unavailable",
                        "error_detail": type(error).__name__,
                    }
                    failures += 1
                emit(
                    status_document(
                        cycle=cycle,
                        options=options,
                        claimed=outcome["claimed"],
                        published=outcome["published"],
                        failed=outcome["failed"],
                        totals=totals,
                        pending=pending_stats(conn),
                        error_code=outcome["error_code"],
                        error_detail=outcome["error_detail"],
                    )
                )
                if options.max_batches and cycle >= options.max_batches:
                    return 0
                if stop is not None and stop.is_set():
                    return 0
                delay = options.interval_s
                if failures:
                    # 退避封顶：失败时不把忙循环打在 NATS 或 PostgreSQL 上。
                    delay = min(options.interval_s * (2 ** min(failures, 6)), 30.0)
                if stop is None:
                    await asyncio.sleep(delay)
                else:
                    await _sleep_or_stop(stop, delay)
    finally:
        await client.close()


async def _sleep_or_stop(stop, delay: float) -> None:
    """可被 stop 打断的睡眠：常驻进程收到 SIGTERM 后不该等满一个退避周期。"""
    deadline = time.monotonic() + delay
    while not stop.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def describe_target(database_url: str) -> str:
    """只回 host:port/db（凭据不进日志），用于 ready 行与验收输出。"""
    try:
        info = psycopg.conninfo.conninfo_to_dict(database_url)
        return f"{info.get('host', '?')}:{info.get('port', '?')}/{info.get('dbname', '?')}"
    except Exception:  # noqa: BLE001 - 描述失败不是失败，且绝不回显原文
        return "unparsable"


def json_line(document: dict) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=False)


async def connect_bounded(nats_module, nats_url: str, options: RelayOptions):
    """有界启动连接：NATS 起不来时**显式失败**，不进入"重连到天荒地老"的半启动状态。

    nats-py 的 `max_reconnect_attempts=-1` 连**首次**连接也是无限重试
    （`_select_next_server` 只在 `max_reconnect_attempts > 0` 时才放弃服务器），所以只靠库
    参数的写法在"NATS 地址填错/服务没起"时表现为进程静默挂着、一行状态都不打——这是最难
    排查的一类失败，也违反了本服务"失败必须显式"的口径。这里给启动加一个上限，连上之后
    仍然把稳态断线交给库做无限重连：NATS 重启不该让常驻进程退出，而发布失败会落进
    `failed_total` / `pending` 并且**不会**写 `published_at`。
    """
    try:
        return await asyncio.wait_for(
            nats_module.connect(
                nats_url,
                name="sensoryplex-relay",
                max_reconnect_attempts=-1,
                reconnect_time_wait=1,
                connect_timeout=options.connect_timeout_s,
            ),
            timeout=options.connect_timeout_s * 2,
        )
    except TimeoutError as error:
        raise RelayError("nats_unreachable", type(error).__name__) from error
    except Exception as error:  # noqa: BLE001 - 连接失败一律按"不可达"上报，类名进 detail
        raise RelayError("nats_unreachable", type(error).__name__) from error
