"""index-worker 常驻消费的契约测试：引用解析、durable 契约比较、可编码文本往返、状态行形状。

测的是**纯判定与形状**：不连 NATS、不连 PostgreSQL、不写向量、不加载权重。真实 JetStream
投递与真实 ack/nak/fail-stop 由 `tests/integration/test_index_consumer.py`（真实 PostgreSQL +
真实 JetStream）承担；真实 BGE 权重与真实 Milvus 由 `tools/verify_index_consume.py` 承担。
契约测试通过**不等于**事件已经被消费成向量。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from edge_material_sdk import PluginError
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from sensoryplex_index_worker import consumer
from sensoryplex_relay import residency

STREAM = "sensoryplex-events"
PREFIX = "sensoryplex.events"

RELEASES = {
    "model_release_id": "bge:bge-small-zh-v1.5@aaaaaaaaaaaa",
    "name": "bge-small-zh-v1.5",
    "version": "main",
    "artifact_hash": "sha256:" + "a" * 64,
    "backend": "CPUExecutionProvider",
    "config_hash": "sha256:" + "b" * 64,
}


def envelope(payload_ref: str = "material:m1:1", event_type: str = "material.upserted") -> bytes:
    return EventEnvelope(
        event_id="material:m1:1",
        event_type=event_type,
        stream_id="stream_contract",
        trace_id="trace_contract",
        payload_ref=payload_ref,
        created_at_unix_ms=1_700_000_000_000,
        schema_version=1,
    ).SerializeToString(deterministic=True)


def facts(**overrides) -> consumer.ObservationFacts:
    fields = {
        "observation_id": "obs_contract",
        "modality": consumer.EMBEDDABLE_MODALITY,
        "payload": {"blocks": [{"text": "财务季度报告"}]},
    }
    fields.update(overrides)
    return consumer.ObservationFacts(**fields)


# ── 订阅面与参数面 ─────────────────────────────────────────────────────────


def test_consumer_binds_to_one_event_type_not_a_wildcard():
    """精确 subject：通配订阅会把另一种事件当成"待消费的素材"反复重投。"""
    options = consumer.ConsumerOptions()
    assert options.subject == "sensoryplex.events.material.upserted"
    assert options.stream == STREAM
    assert options.subject_prefix == PREFIX
    assert "*" not in options.subject and ">" not in options.subject


def test_dedupe_scope_is_the_durable_name():
    """去重表的作用域就是 durable：换名=换一个消费视角，两套账互不顶掉（JetStream 语义）。"""
    options = consumer.ConsumerOptions(durable="index-sink-a")
    assert options.consumer_name == "index-sink-a"
    assert consumer.ConsumerOptions().consumer_name == consumer.DEFAULT_DURABLE


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"durable": ""}, "invalid_durable"),
        ({"durable": "x" * 129}, "invalid_durable"),
        ({"batch": 0}, "invalid_batch"),
        ({"batch": consumer.MAX_BATCH + 1}, "invalid_batch"),
        ({"ack_wait_s": 0}, "invalid_ack_wait"),
        ({"ack_wait_s": consumer.MAX_ACK_WAIT_S + 1}, "invalid_ack_wait"),
        ({"fetch_timeout_s": 0}, "invalid_fetch_timeout"),
        ({"max_deliver": 0}, "invalid_max_deliver"),
        ({"max_deliver": consumer.MAX_DELIVER_BOUND + 1}, "invalid_max_deliver"),
        ({"nak_delay_s": 0}, "invalid_nak_delay"),
        ({"connect_timeout_s": 0}, "invalid_connect_timeout"),
        ({"idle_exit_cycles": -1}, "invalid_idle_exit_cycles"),
        ({"idle_exit_cycles": consumer.MAX_IDLE_EXIT_CYCLES + 1}, "invalid_idle_exit_cycles"),
        ({"stream": ""}, "invalid_stream_name"),
        ({"subject_prefix": "sensoryplex.events."}, "invalid_subject_prefix"),
        ({"subject_prefix": "sensoryplex.>"}, "invalid_subject_prefix"),
        ({"subject_prefix": "sensoryplex.*"}, "invalid_subject_prefix"),
    ],
)
def test_options_reject_out_of_range_values(overrides, code):
    with pytest.raises(consumer.ConsumerError) as failure:
        consumer.ConsumerOptions(**overrides).validate()
    assert failure.value.code == code


def test_default_options_are_valid_and_bounded():
    consumer.ConsumerOptions().validate()
    assert consumer.ConsumerOptions(durable="d", idle_exit_cycles=2).idle_exit_cycles == 2


# ── 受控引用 ───────────────────────────────────────────────────────────────


def test_payload_ref_parses_the_controlled_reference():
    assert consumer.parse_payload_ref("material:m1:1") == consumer.MaterialRef("m1", 1)
    # 素材 id 里含 `:` 也解得出来：从右往左切两刀，revision 必须真的是正整数。
    assert consumer.parse_payload_ref("material:mu:1:7") == consumer.MaterialRef("mu:1", 7)


@pytest.mark.parametrize(
    "payload_ref",
    [
        "",
        "material:m1",
        "material:m1:0",
        "material:m1:-1",
        "material:m1:x",
        "material::1",
        "other:m1:1",
    ],
)
def test_payload_ref_rejects_every_other_shape(payload_ref):
    """形状不对就拒绝：把 `material:m1:x` 当成"查不到素材"会把契约缺陷伪装成数据缺失。"""
    with pytest.raises(consumer.ConsumerError) as failure:
        consumer.parse_payload_ref(payload_ref)
    assert failure.value.code == "invalid_payload_ref"
    assert len(failure.value.detail) <= 80


# ── durable 契约 ───────────────────────────────────────────────────────────


def snapshot(**overrides) -> dict:
    fields = {
        "durable_name": consumer.DEFAULT_DURABLE,
        "filter_subject": "sensoryplex.events.material.upserted",
        "ack_policy": "explicit",
        "ack_wait": consumer.DEFAULT_ACK_WAIT_S,
        "max_deliver": consumer.DEFAULT_MAX_DELIVER,
        "max_ack_pending": consumer.DEFAULT_BATCH,
    }
    fields.update(overrides)
    return fields


def test_consumer_contract_diff_is_empty_when_everything_matches():
    assert consumer.consumer_contract_diff(snapshot(), consumer.ConsumerOptions()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"durable_name": "other"},
        {"filter_subject": "sensoryplex.events.>"},
        {"ack_policy": "none"},
        {"ack_wait": 1.0},
        {"max_deliver": consumer.DEFAULT_MAX_DELIVER + 1},
        {"max_ack_pending": 1},
    ],
)
def test_consumer_contract_diff_reports_every_drift(overrides):
    """`max_deliver` 一起比是刻意的：被改大就把 fail-stop 变成潜在死循环。"""
    diff = consumer.consumer_contract_diff(snapshot(**overrides), consumer.ConsumerOptions())
    assert len(diff) == 1, diff
    assert "!=" in diff[0] or "ack_policy" in diff[0]


def test_consumer_snapshot_normalizes_enum_and_string_forms():
    """`AckPolicy.EXPLICIT` 与 `"explicit"` 是同一件事：形状不同不该报成漂移（假警报）。"""
    enum_like = SimpleNamespace(
        config=SimpleNamespace(
            durable_name=consumer.DEFAULT_DURABLE,
            filter_subject="sensoryplex.events.material.upserted",
            ack_policy=SimpleNamespace(value="explicit"),
            ack_wait=consumer.DEFAULT_ACK_WAIT_S,
            max_deliver=consumer.DEFAULT_MAX_DELIVER,
            max_ack_pending=consumer.DEFAULT_BATCH,
        )
    )
    assert (
        consumer.consumer_contract_diff(
            consumer.consumer_snapshot(enum_like), consumer.ConsumerOptions()
        )
        == []
    )


def test_consumer_config_is_explicit_and_bounded():
    import nats.js.api as jsapi

    options = consumer.ConsumerOptions(durable="d", batch=7, max_deliver=3, ack_wait_s=11.5)
    config = consumer.consumer_config(options)
    assert config.durable_name == "d"
    assert config.filter_subject == "sensoryplex.events.material.upserted"
    assert config.ack_policy is jsapi.AckPolicy.EXPLICIT
    assert config.ack_wait == 11.5
    assert config.max_deliver == 3
    # 在飞上限跟着批大小：有界，不允许"无限在飞"。
    assert config.max_ack_pending == 7


class FakeJetStream:
    """只实现 ensure_consumer 用到的两件事：查 durable、建 durable。"""

    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    async def consumer_info(self, stream, durable):
        if self.existing is None:
            raise type("NotFoundError", (Exception,), {})("consumer not found")
        return SimpleNamespace(config=SimpleNamespace(**self.existing))

    async def add_consumer(self, stream, config=None):
        self.added.append((stream, config))

    async def pull_subscribe(self, subject, durable=None, stream=None):  # pragma: no cover
        raise AssertionError("not used here")


def test_ensure_consumer_creates_only_when_missing():
    js = FakeJetStream()
    asyncio.run(consumer.ensure_consumer(js, consumer.ConsumerOptions()))
    assert len(js.added) == 1
    assert js.added[0][0] == STREAM
    assert js.added[0][1].max_deliver == consumer.DEFAULT_MAX_DELIVER


def test_ensure_consumer_accepts_a_matching_durable_without_touching_it():
    js = FakeJetStream(existing=snapshot())
    asyncio.run(consumer.ensure_consumer(js, consumer.ConsumerOptions()))
    assert js.added == []


def test_ensure_consumer_reports_drift_instead_of_rewriting():
    js = FakeJetStream(existing=snapshot(max_deliver=consumer.DEFAULT_MAX_DELIVER + 5))
    with pytest.raises(consumer.ConsumerError) as failure:
        asyncio.run(consumer.ensure_consumer(js, consumer.ConsumerOptions()))
    assert failure.value.code == "event_consumer_contract_mismatch"
    assert js.added == []


# ── 可编码文本：与插件同一份实现 ───────────────────────────────────────────


def test_payload_jsonb_round_trips_into_the_plugin_text_contract():
    """还原成真的 `google.protobuf.Struct`：读 payload 的规则只有插件那一份实现。"""
    upstream = consumer.upstream_observation(facts())
    source = consumer.bge_text.collect_text(upstream)
    assert source.text == "财务季度报告"
    assert source.block_count == 1
    assert source.source_modality == consumer.EMBEDDABLE_MODALITY


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 没有 blocks
        {"blocks": []},  # 一段文字都没有
        {"blocks": [{"text": "   "}]},  # 全是空白
        {"blocks": [{"no_text": 1}]},  # 块里没有 text
        {"blocks": [{"text": 42}]},  # text 不是字符串
        {"blocks": [{"text": "x"}] * (consumer.bge_text.MAX_TEXTS + 1)},  # 块数越界
        {"blocks": [{"text": "好" * (consumer.bge_text.MAX_TOTAL_CHARS + 1)}]},  # 字符数越界
    ],
)
def test_unencodable_payloads_are_rejected_not_truncated(payload):
    upstream = consumer.upstream_observation(facts(payload=payload))
    with pytest.raises(PluginError):
        consumer.bge_text.collect_text(upstream)


# ── 投递与账目形状 ─────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, headers=None, data=b"", num_delivered=1):
        self.headers = headers
        self.data = data
        self.metadata = SimpleNamespace(num_delivered=num_delivered)


def test_message_event_id_requires_the_dedupe_key():
    assert consumer.message_event_id(FakeMessage(headers={"Nats-Msg-Id": "material:m1:1"})) == (
        "material:m1:1"
    )
    for headers in (None, {}, {"Nats-Msg-Id": ""}):
        with pytest.raises(consumer.ConsumerError) as failure:
            consumer.message_event_id(FakeMessage(headers=headers))
        assert failure.value.code == "event_message_id_missing"


def test_delivered_count_reads_the_jetstream_metadata():
    assert consumer.delivered_count(FakeMessage(num_delivered=3)) == 3
    assert consumer.delivered_count(SimpleNamespace()) == 0


def test_status_document_carries_counts_and_no_payload():
    # 在飞深度（本档上限）必须 >= 声明值，否则状态行是"准入失败"而不是一份真实状态行。
    options = consumer.ConsumerOptions(batch=16)
    document = consumer.status_document(
        cycle=2,
        options=options,
        backpressure=residency.read_event_backpressure(
            options.batch,
            environ={residency.TIER_VAR: "small", residency.CAPACITY_VAR: "16"},
        ),
        received=3,
        consumed=2,
        skipped=1,
        failed=0,
        embedded=4,
        totals={"consumed": 2, "duplicate": 1, "skipped": 1, "failed": 0},
        error_code="event_consume_failed",
        error_detail="RuntimeError",
    )
    assert set(document) == {
        "event",
        "cycle",
        "stream",
        "subject",
        "durable",
        "received",
        "consumed",
        "skipped",
        "failed",
        "embedded_total",
        "consumed_total",
        "duplicate_total",
        "skipped_total",
        "failed_total",
        "error_code",
        "error_detail",
        # 分级背压（ADR-027）：四个字段一起出现，缺一个都说明准入没被接上。
        "inflight_state",
        "inflight_declared",
        "inflight_capacity",
        "resident_tier",
    }
    text = json.dumps(document, ensure_ascii=False)
    for leaked in ("财务", "material:m1", "postgresql://", "/Users/", "vector", "blocks"):
        assert leaked not in text
    assert document["event"] == "consume.status"
    assert document["inflight_state"] == "admitted"


def test_json_line_is_stable_for_log_grepping():
    assert consumer.json_line({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert json.loads(consumer.json_line({"a": "中文"})) == {"a": "中文"}


def test_consume_exit_defaults_to_success_and_names_the_stop_reason():
    assert consumer.ConsumeExit().code == 0
    exit_state = consumer.ConsumeExit(
        code=consumer.FATAL_EXIT_CODE, fatal_code="event_retry_exhausted", fatal_detail="x"
    )
    assert exit_state.code == 3
    assert exit_state.fatal_code == "event_retry_exhausted"
