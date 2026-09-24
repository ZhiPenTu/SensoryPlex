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
import time
from dataclasses import dataclass

import psycopg

from . import contract
from .contract import (
    DEFAULT_STREAM,
    DEFAULT_SUBJECT_PREFIX,
    STREAM_DUPLICATE_WINDOW_S,
    STREAM_MAX_AGE_S,
    STREAM_MAX_BYTES,
    STREAM_MAX_MSGS,
    EventBusError,
    EventScope,
    ensure_stream,
    envelope_of,
    stream_contract_diff,
    subject_for,
)
from .residency import EventBackpressure

# 事件总线契约（stream / subject / 保留策略 / envelope 对账）只在 `contract.py` 一处定义，
# 发布端与消费端取的是同一份常量。这里 import 进本模块，让既有读者与测试继续用
# `relay.subject_for` / `relay.stream_contract_diff` 这套命名空间，行为一字未变。
#
# `RelayError` 是历史名字：类型仍是契约里的 `EventBusError`，错误码体系只有一套。
RelayError = EventBusError
EVENT_TYPE_PATTERN = contract.EVENT_TYPE_PATTERN
MAX_EVENT_TYPE_LENGTH = contract.MAX_EVENT_TYPE_LENGTH

MAX_BATCH = 1000
DEFAULT_BATCH = 200

# 公开命名空间：契约里搬过来的常量在这里**有意**重新导出（`sensoryplex_relay.relay` 是发布端
# 读者与既有测试用的入口），所以列进 `__all__`，而不是让 lint 把它们当成没用的 import。
__all__ = [
    "DEFAULT_BATCH",
    "DEFAULT_STREAM",
    "DEFAULT_SUBJECT_PREFIX",
    "EVENT_TYPE_PATTERN",
    "MAX_BATCH",
    "MAX_EVENT_TYPE_LENGTH",
    "STREAM_DUPLICATE_WINDOW_S",
    "STREAM_MAX_AGE_S",
    "STREAM_MAX_BYTES",
    "STREAM_MAX_MSGS",
    "EventBusError",
    "EventScope",
    "RelayError",
    "RelayOptions",
    "claim_candidates",
    "connect_bounded",
    "describe_target",
    "ensure_stream",
    "envelope_of",
    "json_line",
    "mark_published",
    "pending_stats",
    "publish_event",
    "record_failure",
    "relay_cycle",
    "run_relay",
    "status_document",
    "stream_contract_diff",
    "subject_for",
]


@dataclass(frozen=True)
class RelayOptions(EventScope):
    """发布端的参数面；`stream` / `subject_prefix` 的校验规则继承契约里的那一份。"""

    batch: int = DEFAULT_BATCH
    interval_s: float = 1.0
    publish_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    max_batches: int = 0  # 0 = 常驻

    def validate(self) -> None:
        super().validate()
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
    backpressure: EventBackpressure,
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
        # 分级背压（ADR-027）：本进程每轮认领深度的准入结论（声明值 / 上限 / 档位）。
        **backpressure.document(),
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
    *,
    database_url: str,
    nats_url: str,
    options: RelayOptions,
    backpressure: EventBackpressure,
    emit,
    stop=None,
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
                    **backpressure.document(),
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
                        backpressure=backpressure,
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
    """发布端的有界启动连接；实现与消费端共用 `contract.connect_bounded`（ADR-025）。"""
    return await contract.connect_bounded(
        nats_module, nats_url, name="sensoryplex-relay", timeout_s=options.connect_timeout_s
    )
