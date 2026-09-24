"""常驻消费的集成测试：真实 PostgreSQL + 真实迁移（+ 真实 JetStream 投递）。

这一层专治"只有真库/真总线能暴露"的缺陷：SQL 里的列名、幂等键的作用域、`ack` 之后消息
是不是真的从队列里消失、坏事件重投到上限是不是真的 **fail-stop**——纯函数契约测试会全绿，
这里会直接红。

本文件里向量库与 BGE 编码器是**测试替身**（`MemoryIndex` / `FixedEncoder`）：本文件通过
**不等于**向量真的写进了 Milvus，那由 `tools/verify_index_consume.py`（真实 BGE 权重 +
真实 Milvus Lite + 真实 relay + 真实检索面）承担。没有 `SENSORYPLEX_TEST_NATS_URL` 时，
JetStream 相关的用例显式 skip（跳过不算证据）。
"""

import asyncio
import os
import uuid

import psycopg
import pytest
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_index_worker import consumer
from sensoryplex_relay import residency

from tools.migrate import migrate

pytestmark = pytest.mark.integration

OWNER = "index-consume-owner"
SOURCE = "source_index_consume"
STREAM = "stream_index_consume"
MATERIAL = "material_index_consume"
KEY = "material_text_bge_small_zh_v1_5_d512_v1"
DIGEST = "sha256:" + "e" * 64
OCR_RELEASE = "model_ocr_consume"
RELEASES = {
    "model_release_id": "bge:bge-small-zh-v1.5@ffffffffffff",
    "name": "bge-small-zh-v1.5",
    "version": "main",
    "artifact_hash": "sha256:" + "a" * 64,
    "backend": "CPUExecutionProvider",
    "config_hash": "sha256:" + "b" * 64,
}


class MemoryIndex:
    """内存向量库替身：只实现 sink 用到的那几个方法，语义与 Milvus 契约一致。"""

    def __init__(self, vector_index_key: str = KEY):
        self.vector_index_key = vector_index_key
        self.collection = "memory"
        self.dimension = 512
        self.rows: dict[str, dict] = {}

    def ensure_collection(self) -> None:
        return None

    def upsert(self, records: list[dict]) -> int:
        for row in records:
            self.rows[row["embedding_id"]] = dict(row)
        return len(records)

    def fetch(self, embedding_id: str) -> dict | None:
        return self.rows.get(embedding_id)

    def close(self) -> None:
        return None


class FixedEncoder:
    """确定性编码器替身：维度、身份与 release 都显式写出来，便于对账。"""

    release_id = RELEASES["model_release_id"]
    dimension = 512
    vector_index_key = KEY

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, text: str) -> list[float]:
        self.encoded.append(text)
        return [0.0] * (self.dimension - 1) + [1.0]

    def provenance(self) -> dict:
        return dict(RELEASES)


@pytest.fixture
def database():
    url = os.getenv("SENSORYPLEX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set SENSORYPLEX_TEST_DATABASE_URL to run real PostgreSQL integration tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(url, options=f"-c search_path={schema},public")
    try:
        migrate(isolated)
        with psycopg.connect(isolated) as conn:
            conn.execute(
                "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
                "VALUES (%s,'file','[redacted]',%s)",
                (SOURCE, OWNER),
            )
            conn.execute(
                "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
                "VALUES (%s,%s,now(),'stopped')",
                (STREAM, SOURCE),
            )
            conn.execute(
                "INSERT INTO model_release(model_release_id,name,version,artifact_hash,"
                "backend,config_hash) VALUES (%s,%s,%s,%s,%s,%s)",
                (OCR_RELEASE, "rapidocr", "1.0", DIGEST, "cpu", DIGEST),
            )
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def insert_material(
    conn, revision: int = 1, *, material: str = MATERIAL, status: str = "fast_ready"
):
    conn.execute(
        "INSERT INTO material_unit(material_unit_id,revision,stream_id,start_ms,end_ms,status,"
        "search_text,contract_bytes,content_hash) VALUES (%s,%s,%s,0,1000,%s,'',%s,%s)",
        (material, revision, STREAM, status, b"", DIGEST),
    )


def insert_observation(conn, *, observation_id: str, modality: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
        "VALUES (%s,%s,'video_frame',0,1000) ON CONFLICT (item_id) DO NOTHING",
        (observation_id, STREAM),
    )
    conn.execute(
        "INSERT INTO observation(observation_id,item_id,modality,payload_jsonb,confidence,"
        "model_release_id,contract_bytes) VALUES (%s,%s,%s,%s,NULL,%s,%s)",
        (
            observation_id,
            observation_id,
            modality,
            psycopg.types.json.Jsonb(payload),
            OCR_RELEASE,
            b"obs",
        ),
    )


def link_observation(conn, *, observation_id: str, revision: int = 1, material: str = MATERIAL):
    conn.execute(
        # `role` 与写侧同一口径（API 的 append_material 也把 modality 写进这一列）：
        # 它是 NOT NULL，漏掉会以 NotNullViolation 的形式出现在这里，而不是静默变成空串。
        "INSERT INTO material_observation(material_unit_id,revision,observation_id,role) "
        "VALUES (%s,%s,%s,%s)",
        (material, revision, observation_id, "ocr_blocks"),
    )


def seed_material(conn, *, revision: int = 1, material: str = MATERIAL) -> str:
    """一条素材 + 一条可编码观测：消费侧要回查到的就是这两行。"""
    insert_material(conn, revision, material=material)
    observation_id = f"obs_ocr_{material}"
    insert_observation(
        conn,
        observation_id=observation_id,
        modality=consumer.EMBEDDABLE_MODALITY,
        payload={"blocks": [{"text": f"财务季度报告 {material}"}]},
    )
    link_observation(conn, observation_id=observation_id, revision=revision, material=material)
    return observation_id


def envelope_for(material: str = MATERIAL, revision: int = 1, **overrides) -> EventEnvelope:
    event_id = f"material:{material}:{revision}"
    fields = {
        "event_id": event_id,
        "event_type": consumer.SUPPORTED_EVENT_TYPE,
        "stream_id": STREAM,
        "trace_id": "trace_index_consume",
        "payload_ref": event_id,
        "created_at_unix_ms": 1_700_000_000_000,
        "schema_version": 1,
    }
    fields.update(overrides)
    return EventEnvelope(**fields)


def build(
    database: str,
    *,
    material: str = MATERIAL,
    revision: int = 1,
    index: MemoryIndex | None = None,
    encoder: FixedEncoder | None = None,
    options: consumer.ConsumerOptions | None = None,
    **overrides,
):
    index = index or MemoryIndex()
    encoder = encoder or FixedEncoder()
    options = options or consumer.ConsumerOptions()
    with psycopg.connect(database) as conn:
        report = consumer.consume_event(
            conn,
            index,
            encoder,
            event_id=f"material:{material}:{revision}",
            envelope=envelope_for(material, revision, **overrides),
            options=options,
        )
    return report, index, encoder


# ── sink 侧：真库上的记账与幂等 ─────────────────────────────────────────────


def test_consume_event_writes_ready_rows_and_records_the_event(database):
    with psycopg.connect(database) as conn:
        observation_id = seed_material(conn)
    report, index, encoder = build(database)
    assert report.consumed is True
    assert report.observations == 1
    assert report.embedded == 1
    assert report.skipped_modality == 0
    assert encoder.encoded == [f"财务季度报告 {MATERIAL}"]
    with psycopg.connect(database) as conn:
        state = record_state(conn, observation_id)
        assert state["state"] == "ready"
        assert state["error_code"] is None
        # `vector_ref` 是逻辑引用：不含主机路径、不含向量库端口。
        assert state["vector_ref"] == f"milvus://memory/{state and _embedding_id(observation_id)}"
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 1
        assert consumer_consumed(conn) == 1
        # 模型身份是**登记**进事实库的，不是凭空引用一个外键。
        assert conn.execute(
            "SELECT name,version,backend FROM model_release WHERE model_release_id=%s",
            (RELEASES["model_release_id"],),
        ).fetchone() == (RELEASES["name"], RELEASES["version"], RELEASES["backend"])
    assert len(index.rows) == 1


def test_replayed_event_is_skipped_without_rewriting(database):
    """重投落到已完成的事件：不重复干活，但必须 ack 掉它（否则队列里永远留着一条）。"""
    with psycopg.connect(database) as conn:
        seed_material(conn)
    first, first_index, _ = build(database)
    second, second_index, second_encoder = build(database, index=first_index)
    assert first.consumed and not first.duplicate
    assert second.consumed and second.duplicate
    assert second.embedded == 0
    assert second_encoder.encoded == []
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 1
        assert consumer_consumed(conn) == 1


def test_missing_facts_stop_before_any_write(database):
    """事实与事件同事务：查不到素材就是写侧缺陷，不许当成"没数据"静默过去。"""
    with pytest.raises(consumer.ConsumerError) as failure:
        build(database, material="material_absent")
    assert failure.value.code == "event_missing_facts"
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 0
        assert consumer_consumed(conn) == 0


def test_unencodable_observation_is_not_silently_dropped(database):
    """空文本给不出向量（插件契约如此）：仓库里不许留下一条 ready，也不许记账。"""
    with psycopg.connect(database) as conn:
        insert_material(conn)
        insert_observation(
            conn,
            observation_id="obs_empty",
            modality=consumer.EMBEDDABLE_MODALITY,
            payload={"blocks": [], "empty_reason": "model_found_no_text"},
        )
        link_observation(conn, observation_id="obs_empty")
    with pytest.raises(consumer.ConsumerError) as failure:
        build(database)
    assert failure.value.code == "observation_text_rejected"
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 0
        assert consumer_consumed(conn) == 0


def test_materials_without_embeddable_modalities_are_consumed_and_counted(database):
    """只有 VLM 观测的素材没有向量可写：这种事件必须被消费掉，而不是永远重投。"""
    with psycopg.connect(database) as conn:
        insert_material(conn)
        insert_observation(
            conn, observation_id="obs_vlm", modality="frame_description", payload={"text": "画面"}
        )
        link_observation(conn, observation_id="obs_vlm")
    report, index, encoder = build(database)
    assert report.consumed is True
    assert report.embedded == 0
    assert report.skipped_modality == 1
    assert encoder.encoded == []
    assert index.rows == {}
    with psycopg.connect(database) as conn:
        assert consumer_consumed(conn) == 1


def test_unsupported_event_type_is_acknowledged_not_retried_forever(database):
    report, _, _ = build(database, event_type="observation.created")
    assert report.consumed is False
    assert report.skipped_reason == "unsupported_event_type"
    with psycopg.connect(database) as conn:
        assert consumer_consumed(conn) == 0


def record_state(conn, observation_id: str) -> dict | None:
    """按确定性 `embedding_id` 读回事实行：sink 的账必须能在库里查得到。"""
    from sensoryplex_index_worker import records

    embedding_id = records.embedding_id_for(observation_id, MATERIAL, 1)
    return records.record_state(conn, embedding_id)


def _embedding_id(observation_id: str) -> str:
    from sensoryplex_index_worker import records

    return records.embedding_id_for(observation_id, MATERIAL, 1)


def consumer_consumed(conn) -> int:
    return int(conn.execute("SELECT count(*) FROM consumed_event").fetchone()[0])


# ── 真 JetStream：投递、ack 与 fail-stop ────────────────────────────────────


@pytest.fixture
def nats_url():
    url = os.getenv("SENSORYPLEX_TEST_NATS_URL")
    if not url:
        pytest.skip("set SENSORYPLEX_TEST_NATS_URL to run real JetStream integration tests")
    return url


def isolated_scope() -> consumer.ConsumerOptions:
    """每个用例自建 stream/subject 前缀：不碰开发用的 `sensoryplex-events`。

    前缀刻意**不在** `sensoryplex.events.>` 之下：JetStream 不允许两个 stream 的 subject
    互相重叠，挂在开发前缀下面会直接 `subjects overlap with an existing stream`。
    """
    token = uuid.uuid4().hex[:10]
    return consumer.ConsumerOptions(
        stream=f"sensoryplex-events-indexconsume-{token}",
        subject_prefix=f"sensoryplex.indexconsume.{token}",
    )


class NatsHarness:
    """真实 JetStream 上的编排：建流、发布、有界消费，用完删流。"""

    def __init__(self, url: str, database: str, options: consumer.ConsumerOptions):
        self.url = url
        self.database = database
        self.options = options
        self.documents: list[dict] = []
        self.index = MemoryIndex()
        self.encoder = FixedEncoder()
        self._client = None

    async def __aenter__(self):
        import nats
        from sensoryplex_relay import contract

        self._client = await nats.connect(self.url, name="index-consume-integration")
        self._js = self._client.jetstream()
        await contract.ensure_stream(self._js, self.options)
        return self

    async def __aexit__(self, *_):
        try:
            await self._js.delete_stream(self.options.stream)
        finally:
            await self._client.close()

    async def publish(self, envelope: EventEnvelope) -> None:
        await self._js.publish(
            self.options.subject,
            envelope.SerializeToString(deterministic=True),
            headers={"Nats-Msg-Id": envelope.event_id},
        )

    async def drain(self, *, max_deliver: int = consumer.DEFAULT_MAX_DELIVER, idle: int = 2):
        """有界消费：拉不到消息连续 idle 轮就退出，避免测试依赖固定的 sleep 时长。"""
        options = consumer.ConsumerOptions(
            stream=self.options.stream,
            subject_prefix=self.options.subject_prefix,
            durable=self.options.durable,
            batch=10,
            fetch_timeout_s=0.25,
            max_deliver=max_deliver,
            nak_delay_s=0.05,
            idle_exit_cycles=idle,
        )
        client, subscription = await consumer.open_consumer(nats_url=self.url, options=options)
        try:
            return await consumer.consume_loop(
                client=client,
                subscription=subscription,
                database_url=self.database,
                options=options,
                # 集成层不注入分级：这里要测的是投递/ack/fail-stop，不是准入（那是契约层的事）。
                backpressure=residency.read_event_backpressure(options.batch, environ={}),
                index=self.index,
                encoder=self.encoder,
                emit=self.documents.append,
            )
        finally:
            await client.close()


def test_jetstream_delivery_reaches_the_sink_and_acks(database, nats_url):
    with psycopg.connect(database) as conn:
        observation_id = seed_material(conn)
    scope = isolated_scope()

    async def scenario():
        async with NatsHarness(nats_url, database, scope) as harness:
            await harness.publish(envelope_for())
            state = await harness.drain()
            info = await harness._js.consumer_info(scope.stream, scope.durable)
            return state, info

    state, info = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert state.code == 0, state
    # ack 真的到了服务端：这条投递不再挂着。
    assert info.num_ack_pending == 0
    with psycopg.connect(database) as conn:
        assert record_state(conn, observation_id)["state"] == "ready"
        assert consumer_consumed(conn) == 1


def test_failed_event_is_retried_and_then_stops_loudly(database, nats_url):
    """坏事件不许被静默丢掉，也不许无限重投：重投到上限后按原因显式停止。"""
    scope = isolated_scope()

    async def scenario():
        async with NatsHarness(nats_url, database, scope) as harness:
            await harness.publish(envelope_for(material="material_absent"))
            state = await harness.drain(max_deliver=2)
            info = await harness._js.consumer_info(scope.stream, scope.durable)
            return state, info, list(harness.documents)

    state, info, documents = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert state.code == consumer.FATAL_EXIT_CODE
    assert state.fatal_code == "event_retry_exhausted"
    assert state.fatal_detail == "event_missing_facts"
    # 没有 ack，也没有记账：这条事件仍然留在队列里等人处理。
    assert info.num_ack_pending >= 1
    assert any(document.get("failed", 0) >= 1 for document in documents)
    # 累计口径也必须动：只报本轮计数时，常驻进程会一直显示 failed_total=0——运维看到的是
    # "从没失败过"，而实际已经失败过若干轮。
    assert any(document.get("failed_total", 0) >= 1 for document in documents)
    with psycopg.connect(database) as conn:
        assert consumer_consumed(conn) == 0
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 0


def test_stream_contract_must_exist_before_consuming(database, nats_url):
    """消费端**绝不**自动建流：那会把"发布端还没部署"伪装成"链路已经通了"。"""
    scope = isolated_scope()

    async def scenario():
        import nats

        client = await nats.connect(nats_url, name="index-consume-absent-stream")
        try:
            with pytest.raises(consumer.EventBusError) as failure:
                await consumer.open_consumer(nats_url=nats_url, options=scope)
            return failure.value.code
        finally:
            await client.close()

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=60)) == "event_stream_missing"
