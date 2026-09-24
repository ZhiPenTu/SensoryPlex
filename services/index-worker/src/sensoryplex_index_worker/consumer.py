"""JetStream → sink 的常驻消费：把 outbox 事件消费成向量库里确认写入的事实（ADR-025）。

这条循环补的是 ADR-024 留下的那一跳：relay 只负责"事件确认发到 JetStream"，本模块负责
"事件被消费成事实"。四条写死的语义：

1. **消费与检索必须在同一个进程里**。Milvus Lite 的数据目录是进程级 flock 独占的
   （ADR-020），所以"持有向量库的进程"全机只能有一个：消费循环因此挂在 `serve` 的同一进程上。
   拆成两个进程不是"更解耦"，而是要么新写入的向量对检索面不可见，要么第二个进程直接
   `vector_store_locked`。服务端 Milvus 形态下才有条件拆开——那是后续拓扑，不是本切片的猜测。
2. **事件只是通知，事实在库里**。`material.upserted` 的 `payload_ref = material:<id>:<rev>`
   是受控引用（ADR-010：控制面不传载荷），可编码文本仍在 `observation.payload_jsonb` 里，
   消费侧按引用回查。回查不到不是"没数据"，而是写侧缺陷——事实与事件同事务，缺了就是错。
3. **先干活后记账**。`consumed_event` 只有"已完成"这一态（ADR-024 §6），所以顺序是
   "编码 → 落库 → 确认写入" → `record_consumed` → `ack`。崩在中间只会让工作重做一遍，
   而 `embedding_id` 是确定性的，重做得到同一份向量。
4. **少一条向量就不算消费完成**。本事件应产出的向量没有全部写出来，事件就**不记账、不 ack**，
   交给 JetStream 重投；重投达到上限时进程以 `event_retry_exhausted` **显式停止**，而不是把
   "丢了一条向量"写成"事件已消费"。死信流（dead-letter）与按原因分流是后续切片。

刻意不做的事：不自动建 stream、不自动建 durable 之外的拓扑——流必须已存在且符合契约
（`require_stream`），因为"消费端凭空建一个流"会把"发布端还没部署"伪装成"链路已经通了"；
不改事实；不把文本、向量、DSN、载荷写进状态行；每批**串行**处理，在飞推理恒为 1
（ADR-019 / ADR-021 的"并发必须有上限"口径，这里是上限取 1 的那一端）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field

import psycopg
from edge_material_plugin_embed_bge_onnx import plugin as bge_plugin
from edge_material_plugin_embed_bge_onnx import text as bge_text
from edge_material_sdk import PluginError
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from google.protobuf.json_format import ParseDict
from sensoryplex_relay.contract import (
    EventBusError,
    EventScope,
    connect_bounded,
    envelope_of,
    require_stream,
    subject_for,
)

from . import records
from .errors import IndexContractError, VectorStoreError
from .query_encoder import QueryEncoderError, stable_code
from .worker import index_embedding

# 本切片只消费这一种事件：relay 的 subject 规则是 `<prefix>.<event_type>`，订阅精确 subject，
# 而不是 `>` 通配——通配订阅会让另一种事件被当成"待消费的素材"反复重投。
SUPPORTED_EVENT_TYPE = "material.upserted"
PAYLOAD_REF_PREFIX = "material:"
DEFAULT_DURABLE = "sensoryplex-index-sink"
DEFAULT_BATCH = 50
MAX_BATCH = 500
DEFAULT_ACK_WAIT_S = 60.0
MAX_ACK_WAIT_S = 3600.0
DEFAULT_FETCH_TIMEOUT_S = 2.0
MAX_FETCH_TIMEOUT_S = 60.0
DEFAULT_MAX_DELIVER = 5
MAX_DELIVER_BOUND = 20
DEFAULT_NAK_DELAY_S = 5.0
MAX_NAK_DELAY_S = 600.0
DEFAULT_CONNECT_TIMEOUT_S = 10.0
MAX_CONNECT_TIMEOUT_S = 120.0
MAX_IDLE_EXIT_CYCLES = 10_000
# 可编码模态与产出模态都取自 BGE 插件：消费侧不另立一套名字，否则"插件进程编码"与
# "消费侧编码"会各自漂移（同 ADR-023 的查询编码器口径）。
EMBEDDABLE_MODALITY = bge_text.INPUT_MODALITY
PRODUCED_MODALITY = bge_plugin.MODALITY
# 消费侧整轮失败的退出码：与"启动参数/契约错"（1）分开，便于运维按码分流。
FATAL_EXIT_CODE = 3


class ConsumerError(Exception):
    """带稳定原因码的消费侧失败；调用方按 code 分支，不解析 detail 文本。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class ConsumerOptions(EventScope):
    """消费侧参数面；`stream` / `subject_prefix` 的校验规则继承契约里的那一份。

    `idle_exit_cycles > 0` 是**有界**运行：连续这么多轮拉不到消息就退出（0 = 常驻）。
    `consumer_name`（去重表作用域）就是 `durable`：换一个 durable 名等于换一个消费视角，
    两套视角的账互不顶掉——这是 JetStream 的语义，不是我这一层新加的规则。
    """

    durable: str = DEFAULT_DURABLE
    batch: int = DEFAULT_BATCH
    ack_wait_s: float = DEFAULT_ACK_WAIT_S
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    max_deliver: int = DEFAULT_MAX_DELIVER
    nak_delay_s: float = DEFAULT_NAK_DELAY_S
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    idle_exit_cycles: int = 0

    def validate(self) -> None:
        try:
            # 契约里那一份 stream/subject 校验照旧是唯一实现；只把错误码折成本层的一族，
            # 调用方就能只按 `ConsumerError` 分支，不必同时认两个异常类型。
            super().validate()
        except EventBusError as error:
            raise ConsumerError(error.code, error.detail) from error
        if not self.durable or len(self.durable) > 128:
            raise ConsumerError("invalid_durable")
        if not 1 <= self.batch <= MAX_BATCH:
            raise ConsumerError("invalid_batch", str(self.batch))
        if not 0 < self.ack_wait_s <= MAX_ACK_WAIT_S:
            raise ConsumerError("invalid_ack_wait", str(self.ack_wait_s))
        if not 0 < self.fetch_timeout_s <= MAX_FETCH_TIMEOUT_S:
            raise ConsumerError("invalid_fetch_timeout", str(self.fetch_timeout_s))
        if not 1 <= self.max_deliver <= MAX_DELIVER_BOUND:
            raise ConsumerError("invalid_max_deliver", str(self.max_deliver))
        if not 0 < self.nak_delay_s <= MAX_NAK_DELAY_S:
            raise ConsumerError("invalid_nak_delay", str(self.nak_delay_s))
        if not 0 < self.connect_timeout_s <= MAX_CONNECT_TIMEOUT_S:
            raise ConsumerError("invalid_connect_timeout", str(self.connect_timeout_s))
        if not 0 <= self.idle_exit_cycles <= MAX_IDLE_EXIT_CYCLES:
            raise ConsumerError("invalid_idle_exit_cycles", str(self.idle_exit_cycles))

    @property
    def subject(self) -> str:
        return subject_for(SUPPORTED_EVENT_TYPE, self.subject_prefix)

    @property
    def consumer_name(self) -> str:
        return self.durable


@dataclass(frozen=True)
class MaterialRef:
    material_unit_id: str
    revision: int


def parse_payload_ref(payload_ref: str) -> MaterialRef:
    """`material:<id>:<rev>` → 受控引用；形状不对就**拒绝**，不猜另一种写法。

    先摘掉固定的 `material:` 前缀，再从右往左切一刀：素材 id 里万一含 `:`（它不是本层管的
    字符集）也仍然解得出来；而 `revision` 必须真的是正整数——把 `material:m1:x` 当成
    "revision 是 x" 去查库，失败会以"查不到素材"的形式出现，把一件契约缺陷伪装成一次数据缺失。
    """
    text = payload_ref or ""
    if not text.startswith(PAYLOAD_REF_PREFIX):
        raise ConsumerError("invalid_payload_ref", text[:80] or "empty")
    material_unit_id, _, revision_text = text[len(PAYLOAD_REF_PREFIX) :].rpartition(":")
    if not material_unit_id or not revision_text:
        raise ConsumerError("invalid_payload_ref", text[:80] or "empty")
    try:
        revision = int(revision_text)
    except ValueError as error:
        raise ConsumerError("invalid_payload_ref", text[:80] or "empty") from error
    if revision <= 0:
        raise ConsumerError("invalid_payload_ref", text[:80] or "empty")
    return MaterialRef(material_unit_id=material_unit_id, revision=revision)


@dataclass(frozen=True)
class ObservationFacts:
    observation_id: str
    modality: str
    payload: dict


@dataclass(frozen=True)
class MaterialFacts:
    material_unit_id: str
    revision: int
    stream_id: str
    start_ms: int
    end_ms: int
    status: str
    observations: tuple[ObservationFacts, ...]


def resolve_material(conn, ref: MaterialRef) -> MaterialFacts | None:
    """按受控引用回查事实（素材 + 它引用到的观测）；素材不存在返回 None 由调用方判定。"""
    row = records.load_material(conn, material_unit_id=ref.material_unit_id, revision=ref.revision)
    if row is None:
        return None
    observations = records.load_observations(
        conn, material_unit_id=ref.material_unit_id, revision=ref.revision
    )
    return MaterialFacts(
        material_unit_id=row["material_unit_id"],
        revision=row["revision"],
        stream_id=row["stream_id"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        status=row["status"],
        observations=tuple(
            ObservationFacts(
                observation_id=item["observation_id"],
                modality=item["modality"],
                payload=item["payload"],
            )
            for item in observations
        ),
    )


def upstream_observation(facts: ObservationFacts) -> material.Observation:
    """把 `payload_jsonb` 还原成插件认识的上游观测形态。

    还原成**真的 `google.protobuf.Struct`**，而不是自己写一层 dict 适配：插件读 payload 的
    规则（`blocks` 在不在、块有没有 `text`、数量与字符数上限）只有一份实现，消费侧必须走
    那一份——否则"插件进程编码"与"消费侧编码"会各自漂移，而且漂移只在部分素材上显现。
    """
    upstream = material.Observation()
    upstream.observation_id = facts.observation_id
    upstream.modality = facts.modality
    ParseDict(facts.payload, upstream.payload)
    return upstream


@dataclass
class SinkReport:
    """一条事件的消费结果（给调用方与测试看；状态行只取其中的计数）。"""

    event_id: str
    event_type: str
    consumed: bool = False
    duplicate: bool = False
    skipped_reason: str = ""
    observations: int = 0
    embedded: int = 0
    skipped_modality: int = 0
    embedding_ids: list[str] = field(default_factory=list)


def consume_event(
    conn,
    index,
    encoder,
    *,
    event_id: str,
    envelope,
    options: ConsumerOptions,
) -> SinkReport:
    """消费一条事件：回查事实 → 编码 → 落库确认 → 记账（ADR-025 的四条语义）。

    本函数**自己决定何时提交**：成功的 sink 在返回前提交（否则 `ack` 就只是"我嘴上说做完了"），
    失败的 sink 先把 `mark_failed` 那一行提交再抛出（失败必须是可查的事实，不能随事务回滚消失）。
    """
    report = SinkReport(event_id=event_id, event_type=envelope.event_type)
    if envelope.event_type != SUPPORTED_EVENT_TYPE:
        # 订阅是精确 subject，理论上到不了这里；真到了就记账跳过，而不是让一条别的事件
        # 把整个消费循环卡死（那会把一个路由问题表现成"链路挂了"）。
        report.skipped_reason = "unsupported_event_type"
        conn.commit()
        return report
    ref = parse_payload_ref(envelope.payload_ref)
    if records.is_consumed(conn, event_id=event_id, consumer_name=options.consumer_name):
        # 重投（或 relay 的重复发布）落到已完成的事件：不重复干活，但要 ack 掉它。
        conn.commit()
        report.duplicate = True
        report.consumed = True
        return report
    facts = resolve_material(conn, ref)
    if facts is None:
        conn.rollback()
        raise ConsumerError("event_missing_facts", "material_not_found")
    report.observations = len(facts.observations)
    model_release_registered = False
    for facts_item in facts.observations:
        if facts_item.modality != EMBEDDABLE_MODALITY:
            # 不是"可编码文本"的观测（VLM / ASR 等）：本 sink 不处理，但要计数——静默跳过会让
            # "这个素材根本没产出向量"变成无从解释的现象。
            report.skipped_modality += 1
            continue
        outcome = _sink_observation(
            conn,
            index,
            encoder,
            facts=facts,
            item=facts_item,
            register_model_release=not model_release_registered,
        )
        model_release_registered = True
        report.embedded += 1
        report.embedding_ids.append(outcome.embedding_id)
    records.record_consumed(conn, event_id=event_id, consumer_name=options.consumer_name)
    conn.commit()
    report.consumed = True
    return report


def _sink_observation(
    conn,
    index,
    encoder,
    *,
    facts: MaterialFacts,
    item: ObservationFacts,
    register_model_release: bool,
):
    """单条观测的 sink：插件文本契约 → 编码 → 维度守卫 → Milvus → 确认 → 事实。

    失败一律抛 `ConsumerError`（带稳定原因码），调用方据此判定"这条事件还没完成"。
    文本契约失败（空文本、块数/字符数越界）发生在**拿到身份之前**，因此不写
    `embedding_record`——与插件进程里同一条失败的表现一致（插件同样不会产出观测）。
    """
    upstream = upstream_observation(item)
    try:
        source = bge_text.collect_text(upstream)
    except PluginError as error:
        # `stable_code` 会把不合形状的串折成通用码：插件原因串里不该出现主机路径。
        raise ConsumerError("observation_text_rejected", stable_code(str(error))) from error
    try:
        vector = encoder.encode(source.text)
    except QueryEncoderError as error:
        raise ConsumerError("observation_encode_failed", error.code) from error
    if register_model_release:
        try:
            records.ensure_model_release(conn, **encoder.provenance())
        except records.IdentityConflict as error:
            raise ConsumerError("model_release_identity_conflict", str(error)) from error
    payload = {
        "embedding_id": item.observation_id,
        "vector": vector,
        "dimension": len(vector),
        "vector_index_key": encoder.vector_index_key,
        "text_sha256": source.text_sha256,
    }
    try:
        outcome = index_embedding(
            conn,
            index,
            payload=payload,
            material_unit_id=facts.material_unit_id,
            material_revision=facts.revision,
            model_release_id=encoder.release_id,
            stream_id=facts.stream_id,
            start_ms=facts.start_ms,
            end_ms=facts.end_ms,
            modality=PRODUCED_MODALITY,
        )
    except (VectorStoreError, IndexContractError) as error:
        # `index_embedding` 已经留下了 failed 行（带原因码），把它提交：否则"拒绝了但库里
        # 查不到"就等于静默丢弃（ADR-020 §2 同一条规则）。
        conn.commit()
        raise ConsumerError("observation_sink_failed", error.code) from error
    return outcome


def _enum_text(value) -> str:
    """枚举取值与字符串取值统一成小写串：nats-py 里 `AckPolicy` 是枚举，服务端读回来又可能是串。

    两边形状不同时用一个 `.lower()` 去比，会把"配置一致"判成漂移——那是**假警报**，
    而假警报会让人开始忽略真警报（漂移检测最贵的失败形态）。
    """
    return str(getattr(value, "value", value) or "").lower()


def consumer_snapshot(info) -> dict:
    """把 nats-py 的 `ConsumerInfo` 折成可比较的字典（`ack_wait` 与保留策略同为秒口径）。"""
    config = info.config
    return {
        "durable_name": config.durable_name,
        "filter_subject": config.filter_subject,
        "ack_policy": _enum_text(config.ack_policy),
        "ack_wait": config.ack_wait,
        "max_deliver": config.max_deliver,
        "max_ack_pending": config.max_ack_pending,
    }


def consumer_contract_diff(existing: dict, options: ConsumerOptions) -> list[str]:
    """已存在的 durable 与本次契约的差异（为空表示一致）。

    `max_deliver` 一起比是刻意的：它决定"坏事件"会不会被无限重投——被改大就把
    fail-stop 变成潜在的死循环，被改小会让重投还没试完就停下，两者都属于静默变质。
    """
    diff: list[str] = []
    if existing.get("durable_name") != options.durable:
        diff.append(f"durable_name={existing.get('durable_name')}!= {options.durable}")
    if existing.get("filter_subject") != options.subject:
        diff.append(f"filter_subject={existing.get('filter_subject')}!= {options.subject}")
    if existing.get("ack_policy") != "explicit":
        diff.append(f"ack_policy={existing.get('ack_policy')}")
    if float(existing.get("ack_wait") or 0) != options.ack_wait_s:
        diff.append(f"ack_wait={existing.get('ack_wait')}!= {options.ack_wait_s}s")
    if int(existing.get("max_deliver") or 0) != options.max_deliver:
        diff.append(f"max_deliver={existing.get('max_deliver')}!= {options.max_deliver}")
    if int(existing.get("max_ack_pending") or 0) != options.batch:
        diff.append(f"max_ack_pending={existing.get('max_ack_pending')}!= {options.batch}")
    return diff


def consumer_config(options: ConsumerOptions):
    """durable 的契约：显式 ack、在飞上限 = 批大小（有界），重投上限 = `max_deliver`。"""
    import nats.js.api as jsapi

    return jsapi.ConsumerConfig(
        durable_name=options.durable,
        filter_subject=options.subject,
        ack_policy=jsapi.AckPolicy.EXPLICIT,
        ack_wait=options.ack_wait_s,
        max_deliver=options.max_deliver,
        max_ack_pending=options.batch,
    )


def _not_found(error: Exception) -> bool:
    return type(error).__name__ == "NotFoundError"


async def ensure_consumer(js, options: ConsumerOptions):
    """建/校验 durable：不存在就按契约建，已存在则**只校验**（漂移即失败）。

    与 stream 不同，durable 是**消费侧自己的**拓扑：它描述"我怎么消费"，发布端不认识它。
    因此这里允许创建，但创建出来的必须与契约一致，且不覆盖已存在的。
    """
    try:
        info = await js.consumer_info(options.stream, options.durable)
    except Exception as error:  # noqa: BLE001 - nats-py 用 NotFoundError 报"不存在"
        if not _not_found(error):
            raise ConsumerError("event_consumer_unavailable", type(error).__name__) from error
        return await js.add_consumer(options.stream, config=consumer_config(options))
    diff = consumer_contract_diff(consumer_snapshot(info), options)
    if diff:
        raise ConsumerError("event_consumer_contract_mismatch", ";".join(diff)[:200])
    return info


async def open_consumer(*, nats_url: str, options: ConsumerOptions):
    """连接 + 对完 stream/durable 契约 + 绑定订阅；任一步失败都关掉连接再抛。

    契约在**启动时**就对完，而不是留到第一条消息：漂移的 stream（保留策略被改小）与漂移的
    durable（重投上限被改）都是配置事实，不是"稍后重试可能成功"的瞬时故障。
    """
    import nats

    options.validate()
    client = await connect_bounded(
        nats, nats_url, name="sensoryplex-index-sink", timeout_s=options.connect_timeout_s
    )
    try:
        js = client.jetstream()
        await require_stream(js, options)
        await ensure_consumer(js, options)
        subscription = await js.pull_subscribe(
            options.subject, durable=options.durable, stream=options.stream
        )
    except BaseException:
        await client.close()
        raise
    return client, subscription


def message_event_id(message) -> str:
    """投递里的 `Nats-Msg-Id`：它就是 outbox 的 `event_id`，也是去重表的主键来源。

    缺失就不接受——没有它就无法把"这一条投递"与事实对账，重投与重复发布也就无从去重。
    """
    headers = getattr(message, "headers", None) or {}
    value = headers.get("Nats-Msg-Id") or ""
    if not value:
        raise ConsumerError("event_message_id_missing", "Nats-Msg-Id")
    return value


def delivered_count(message) -> int:
    metadata = getattr(message, "metadata", None)
    return int(getattr(metadata, "num_delivered", 0) or 0)


@dataclass
class ConsumeExit:
    """消费循环的收尾：退出码 + 触发退出的原因码（常驻形态下要能解释"为什么停了"）。"""

    code: int = 0
    fatal_code: str = ""
    fatal_detail: str = ""


async def consume_loop(
    *,
    client,
    subscription,
    database_url: str,
    options: ConsumerOptions,
    index,
    encoder,
    emit,
    stop=None,
) -> ConsumeExit:
    """常驻（或 `idle_exit_cycles` 轮）消费循环；每条消息的 ack/nak 都有依据。"""
    totals = {"consumed": 0, "duplicate": 0, "skipped": 0, "failed": 0, "embedded": 0}
    cycle = 0
    idle_cycles = 0
    last_error_code = ""
    last_error_detail = ""
    with psycopg.connect(database_url) as conn:
        while True:
            if stop is not None and stop.is_set():
                return ConsumeExit()
            cycle += 1
            received = 0
            cycle_consumed = 0
            cycle_skipped = 0
            cycle_failed = 0
            try:
                messages = await subscription.fetch(options.batch, timeout=options.fetch_timeout_s)
            except Exception as error:  # noqa: BLE001 - 超时=这一轮没有消息，其它按总线故障
                messages = []
                if type(error).__name__ != "TimeoutError":
                    cycle_failed += 1
                    last_error_code, last_error_detail = "event_fetch_failed", type(error).__name__
            for message in messages:
                received += 1
                try:
                    report = consume_message(conn, index, encoder, message, options)
                except ConsumerError as error:
                    conn.rollback()
                    cycle_failed += 1
                    last_error_code, last_error_detail = error.code, error.detail
                    # 契约/事实缺陷：重投不会自愈，但也**不静默丢掉**——重投到上限后显式停止。
                    if delivered_count(message) >= options.max_deliver:
                        return ConsumeExit(
                            code=FATAL_EXIT_CODE,
                            fatal_code="event_retry_exhausted",
                            fatal_detail=error.code,
                        )
                    await _nak(message, options)
                    continue
                except EventBusError as error:
                    conn.rollback()
                    cycle_failed += 1
                    last_error_code, last_error_detail = error.code, error.detail
                    if delivered_count(message) >= options.max_deliver:
                        return ConsumeExit(
                            code=FATAL_EXIT_CODE,
                            fatal_code="event_retry_exhausted",
                            fatal_detail=error.code,
                        )
                    await _nak(message, options)
                    continue
                except psycopg.Error as error:
                    # 元数据库不可用：不臆造"处理完了"，这条不 ack，等重投。
                    conn.rollback()
                    cycle_failed += 1
                    last_error_code, last_error_detail = (
                        "metadata_store_unavailable",
                        type(error).__name__,
                    )
                    await _nak(message, options)
                    continue
                except Exception as error:  # noqa: BLE001 - 未知失败也要有稳定的上报形状
                    conn.rollback()
                    cycle_failed += 1
                    last_error_code = "event_consume_failed"
                    last_error_detail = type(error).__name__
                    await _nak(message, options)
                    continue
                await message.ack()
                if report.duplicate:
                    totals["duplicate"] += 1
                if report.skipped_reason:
                    totals["skipped"] += 1
                    cycle_skipped += 1
                if report.consumed:
                    totals["consumed"] += 1
                    cycle_consumed += 1
                totals["embedded"] += report.embedded
            # 累计失败数：只报本轮计数的话，常驻进程里"这一轮刚好没坏事件"就会让
            # `failed_total` 一直是 0——运维看到的是"从没失败过"，而实际失败过。
            totals["failed"] += cycle_failed
            idle_cycles = idle_cycles + 1 if received == 0 and cycle_failed == 0 else 0
            emit(
                status_document(
                    cycle=cycle,
                    options=options,
                    received=received,
                    consumed=cycle_consumed,
                    skipped=cycle_skipped,
                    failed=cycle_failed,
                    embedded=totals["embedded"],
                    totals=totals,
                    error_code="" if cycle_failed == 0 else last_error_code,
                    error_detail="" if cycle_failed == 0 else last_error_detail,
                )
            )
            if options.idle_exit_cycles and idle_cycles >= options.idle_exit_cycles:
                return ConsumeExit()
            if stop is not None and stop.is_set():
                return ConsumeExit()


def consume_message(conn, index, encoder, message, options: ConsumerOptions) -> SinkReport:
    """把一次投递变成一次 sink 尝试；抛错表示"这一次没成功"（由循环决定 ack/nak）。"""
    event_id = message_event_id(message)
    envelope = envelope_of(message.data, expected_event_id=event_id)
    return consume_event(
        conn, index, encoder, event_id=envelope.event_id, envelope=envelope, options=options
    )


async def _nak(message, options: ConsumerOptions) -> None:
    """延迟重投：不加延迟的 nak 会把"坏事件 + 常驻进程"变成忙循环。"""
    try:
        await message.nak(delay=options.nak_delay_s)
    except Exception:  # noqa: BLE001 - 已经断开连接时 nak 也会失败，不该因此崩掉循环
        pass


def status_document(
    *,
    cycle: int,
    options: ConsumerOptions,
    received: int,
    consumed: int,
    skipped: int,
    failed: int,
    embedded: int,
    totals: dict,
    error_code: str = "",
    error_detail: str = "",
) -> dict:
    """状态行：只放计数、标识与时间，不放载荷、文本、向量、令牌或主机路径。"""
    return {
        "event": "consume.status",
        "cycle": cycle,
        "stream": options.stream,
        "subject": options.subject,
        "durable": options.durable,
        "received": received,
        "consumed": consumed,
        "skipped": skipped,
        "failed": failed,
        "embedded_total": embedded,
        "consumed_total": totals["consumed"],
        "duplicate_total": totals["duplicate"],
        "skipped_total": totals["skipped"],
        "failed_total": totals["failed"],
        "error_code": error_code,
        # 只放稳定原因码与异常**类名**：异常文本可能带地址、DSN 或载荷片段。
        "error_detail": error_detail,
    }


def json_line(document: dict) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=False)


class ConsumerRunner:
    """把常驻消费挂到检索面进程上（Milvus Lite 进程独占：持有者只能有一个，ADR-025）。

    `serve` 在开完向量库后 `start()`。线程先在**有界时间**内把 NATS 连接与 stream/durable
    契约对完，再进入消费循环：对不上就通过 `error` 显式报出来，绝不让进程"看起来在跑"。
    循环以 `FATAL_EXIT_CODE` 结束时调用 `on_fatal`，让进程按原因退出而不是静默降级。
    """

    def __init__(
        self,
        *,
        database_url: str,
        nats_url: str,
        options: ConsumerOptions,
        index,
        encoder,
        emit,
        on_fatal=None,
    ):
        options.validate()
        self.options = options
        self.nats_url = nats_url
        self.exit = ConsumeExit()
        self.error: Exception | None = None
        self._database_url = database_url
        self._index = index
        self._encoder = encoder
        self._emit = emit
        self._on_fatal = on_fatal
        self._startup = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name="sensoryplex-index-consume")

    def start(self) -> None:
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        """等"真的接上了"或"显式失败"；返回 False 只表示超时，失败原因在 `error` 里。"""
        return self._startup.wait(timeout)

    @property
    def fatal_code(self) -> str:
        return self.exit.fatal_code

    def stop(self) -> None:
        """常驻形态的优雅收尾：不 ack 未完成的投递，让它们回到队列里给别人/下次重投。"""
        self._stop.set()
        self._thread.join(timeout=30)

    def _main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as error:  # noqa: BLE001 - 线程里不能把异常吞掉
            self.error = self.error or error
            self.exit = ConsumeExit(
                code=1, fatal_code="consumer_thread_failed", fatal_detail=type(error).__name__
            )
            self._startup.set()

    async def _run(self) -> None:
        try:
            client, subscription = await open_consumer(nats_url=self.nats_url, options=self.options)
        except (EventBusError, ConsumerError) as error:
            self.error = error
            self._startup.set()
            return
        self._emit(
            {
                "event": "consume.ready",
                "stream": self.options.stream,
                "subject": self.options.subject,
                "durable": self.options.durable,
                "batch": self.options.batch,
                "max_deliver": self.options.max_deliver,
                "idle_exit_cycles": self.options.idle_exit_cycles,
            }
        )
        self._startup.set()
        try:
            self.exit = await consume_loop(
                client=client,
                subscription=subscription,
                database_url=self._database_url,
                options=self.options,
                index=self._index,
                encoder=self._encoder,
                emit=self._emit,
                stop=self._stop,
            )
        finally:
            await client.close()
        if self.exit.code:
            self._emit(
                {
                    "event": "consume.fatal",
                    "stream": self.options.stream,
                    "durable": self.options.durable,
                    "exit_code": self.exit.code,
                    "error_code": self.exit.fatal_code,
                    "error_detail": self.exit.fatal_detail,
                }
            )
            if self._on_fatal is not None:
                self._on_fatal()
