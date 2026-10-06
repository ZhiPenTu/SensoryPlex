"""outbox relay 的集成测试：真实 PostgreSQL + 真实迁移（NATS 用替身，真往返在验收脚本里）。

这一层专治"只有真库能暴露"的缺陷：`published_at` 到底在确认之后才写、失败时是不是真的保持
NULL、`attempt` 有没有被记、`ON CONFLICT` 的去重键是不是 `(event_id, consumer_name)`，
以及 `record_failure` 有没有误写 `published_at`。纯函数契约测试全绿也证明不了这些。

JetStream 侧这里用**替身**：本文件验的是记账，不是传输。真实 NATS 的 `Nats-Msg-Id` 去重与
"发布确认后才写"由 `tools/verify_outbox_relay.py` 在真实 JetStream 上承担。
"""

import os
import uuid

import psycopg
import pytest
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_index_worker import records
from sensoryplex_relay import relay

from tools.migrate import migrate

pytestmark = pytest.mark.integration

CONSUMER = "sensoryplex-index-worker"

# created_at 全部写死：`claim_candidates` 的顺序断言不能依赖插入速度。
CREATED_AT = ("2026-09-24T02:00:01Z", "2026-09-24T02:00:02Z", "2026-09-24T02:00:03Z")


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
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class RecordingJetStream:
    """JetStream 替身：只记录"发了什么"，或按需失败。"""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.published: list[tuple[str, str, bytes]] = []

    async def publish(self, subject, payload, headers=None, timeout=None):
        if self.fail:
            raise TimeoutError("publish timeout")
        self.published.append((subject, (headers or {}).get("Nats-Msg-Id", ""), payload))
        return None


def add_event(conn, index: int, *, event_type: str = "material.upserted") -> str:
    event_id = f"material:m{index}:1"
    envelope = EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        stream_id="stream_outbox_integration",
        trace_id="trace_outbox_integration",
        payload_ref=event_id,
        created_at_unix_ms=1_700_000_000_000,
        schema_version=1,
    )
    conn.execute(
        "INSERT INTO event_outbox(event_id,event_type,contract_bytes,created_at) "
        "VALUES (%s,%s,%s,%s)",
        (event_id, event_type, envelope.SerializeToString(deterministic=True), CREATED_AT[index]),
    )
    return event_id


async def run_cycle(conn, js) -> dict:
    """跑一轮 relay；totals 每轮从零起算，断言只看这一轮的增量。"""
    return await relay.relay_cycle(conn, js, relay.RelayOptions(), {"published": 0, "failed": 0})


def outbox_row(conn, event_id: str) -> tuple:
    return conn.execute(
        "SELECT published_at IS NULL, attempt FROM event_outbox WHERE event_id=%s", (event_id,)
    ).fetchone()


# ── 记账：`published_at` 只在确认之后写 ─────────────────────────────────────


async def test_publish_is_recorded_only_after_the_broker_confirms(database):
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        js = RecordingJetStream()
        outcome = await run_cycle(conn, js)
        assert outcome["published"] == 1 and outcome["failed"] == 0
        assert js.published[0][0] == "sensoryplex.events.material.upserted"
        assert js.published[0][1] == event_id  # Nats-Msg-Id 就是 outbox 的 event_id
        unpublished, attempt = outbox_row(conn, event_id)
        assert unpublished is False and attempt == 1
        assert relay.pending_stats(conn)["pending"] == 0


async def test_a_failed_publish_leaves_the_event_unpublished_and_counts_the_attempt(database):
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        js = RecordingJetStream(fail=True)
        outcome = await run_cycle(conn, js)
        assert outcome["published"] == 0 and outcome["failed"] == 1
        assert outcome["error_code"] == "event_publish_failed"
        assert outcome["error_detail"] == "TimeoutError"
        # 这是整套语义里最要命的一条：发失败时**不许**写 published_at，否则事件永久消失。
        unpublished, attempt = outbox_row(conn, event_id)
        assert unpublished is True and attempt == 1
        stats = relay.pending_stats(conn)
        assert stats["pending"] == 1 and stats["max_attempt"] == 1
        assert stats["oldest_pending_age_s"] is not None


async def test_a_contract_defect_is_refused_before_it_reaches_the_broker(database):
    with psycopg.connect(database) as conn:
        # 行里的 event_type 非法（subject 里会被当成通配）：必须**不发**、留在未发布状态。
        event_id = add_event(conn, 0, event_type="material.upserted.*")
        js = RecordingJetStream()
        outcome = await run_cycle(conn, js)
        assert js.published == []
        assert outcome["failed"] == 1 and outcome["error_code"] == "invalid_event_type"
        assert outbox_row(conn, event_id)[0] is True


async def test_a_mismatched_envelope_is_refused(database):
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        envelope = EventEnvelope(
            event_id="material:someone-else:1",
            event_type="material.upserted",
            stream_id="stream_outbox_integration",
            created_at_unix_ms=1_700_000_000_000,
            schema_version=1,
        )
        conn.execute(
            "UPDATE event_outbox SET contract_bytes=%s WHERE event_id=%s",
            (envelope.SerializeToString(deterministic=True), event_id),
        )
        js = RecordingJetStream()
        outcome = await run_cycle(conn, js)
        assert js.published == []
        assert outcome["error_code"] == "event_envelope_mismatch"
        assert outbox_row(conn, event_id)[0] is True


async def test_one_bad_event_does_not_block_the_rest_of_the_batch(database):
    with psycopg.connect(database) as conn:
        good = add_event(conn, 0)
        bad = add_event(conn, 1, event_type="Bad.Upper")
        later = add_event(conn, 2)
        js = RecordingJetStream()
        outcome = await run_cycle(conn, js)
        assert outcome["claimed"] == 3 and outcome["published"] == 2 and outcome["failed"] == 1
        assert [entry[1] for entry in js.published] == [good, later]
        assert outbox_row(conn, good)[0] is False
        assert outbox_row(conn, bad)[0] is True
        assert outbox_row(conn, later)[0] is False


# ── 认领与提交的边界 ────────────────────────────────────────────────────────


def test_claim_takes_the_oldest_unpublished_first_and_changes_nothing(database):
    with psycopg.connect(database) as conn:
        ids = [add_event(conn, index) for index in (2, 0, 1)]
        claimed = relay.claim_candidates(conn, 10)
        assert [row["event_id"] for row in claimed] == [ids[1], ids[2], ids[0]]
        assert claimed[0]["attempt"] == 0
        assert all(outbox_row(conn, event_id)[0] is True for event_id in ids)
        assert relay.claim_candidates(conn, 10)[0]["event_id"] == ids[1]


def test_claim_respects_the_batch_bound(database):
    with psycopg.connect(database) as conn:
        for index in range(3):
            add_event(conn, index)
        assert len(relay.claim_candidates(conn, 2)) == 2
        assert len(relay.claim_candidates(conn, relay.MAX_BATCH)) == 3


def test_mark_published_only_flips_a_row_once(database):
    """认领不互斥（可能重复发布，由 Nats-Msg-Id 吸收），但记账只认第一次。"""
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        assert relay.mark_published(conn, [event_id]) == 1
        assert relay.mark_published(conn, [event_id]) == 0
        assert outbox_row(conn, event_id) == (False, 1)
        assert relay.mark_published(conn, []) == 0


def test_record_failure_never_touches_published_at(database):
    with psycopg.connect(database) as conn:
        published = add_event(conn, 0)
        pending = add_event(conn, 1)
        relay.mark_published(conn, [published])
        assert relay.record_failure(conn, [published, pending]) == 2
        assert outbox_row(conn, published) == (False, 2)
        assert outbox_row(conn, pending) == (True, 1)
        assert relay.record_failure(conn, []) == 0


def test_pending_stats_survives_an_empty_outbox(database):
    with psycopg.connect(database) as conn:
        assert relay.pending_stats(conn) == {
            "pending": 0,
            "oldest_pending_age_s": None,
            "max_attempt": 0,
        }


# ── sink 侧的消费去重（ADR-024 §6） ────────────────────────────────────────


def test_consumed_event_is_idempotent_per_consumer(database):
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        assert records.is_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is False
        assert records.consumed_state(conn, event_id=event_id, consumer_name=CONSUMER) is None
        assert records.record_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True
        assert records.is_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True
        # 重投导致的重复完成是幂等的，不是错误。
        assert records.record_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is False
        state = records.consumed_state(conn, event_id=event_id, consumer_name=CONSUMER)
        assert state["event_id"] == event_id and state["consumed_at"]


def test_consumption_is_scoped_per_consumer(database):
    """主键是 `(event_id, consumer_name)`：第二个消费者必须还能处理同一条事件。"""
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        assert records.record_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True
        second = records.record_consumed(conn, event_id=event_id, consumer_name="another-consumer")
        assert second is True
        assert records.is_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True
        assert conn.execute("SELECT count(*) FROM consumed_event").fetchone()[0] == 2


def test_recording_a_consumption_does_not_require_the_event_to_be_published(database):
    """JetStream 里的消息才是下游的事实来源，`event_outbox` 只是发布侧的账；
    消费记账因此**不**与 outbox 行挂钩（也没有外键），未发布的行照样能记。"""
    with psycopg.connect(database) as conn:
        event_id = add_event(conn, 0)
        assert outbox_row(conn, event_id)[0] is True
        assert records.record_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True
        assert records.is_consumed(conn, event_id=event_id, consumer_name=CONSUMER) is True


def test_outbox_archival_and_purge_lifecycle(database):
    """已确认发布且超过安全窗口的事件与任务被移至归档表，未发布与近期事件保留。"""
    from sensoryplex_relay.archival import archive_and_purge_outbox

    with psycopg.connect(database) as conn:
        # 1. 插入 event_outbox:
        # - evt_old: 10 天前已发布 (应该被归档)
        # - evt_recent: 2 天前已发布 (应该保留)
        # - evt_pending: 10 天前未发布 (应该保留)
        conn.execute(
            """
            INSERT INTO event_outbox
                (event_id, event_type, contract_bytes, published_at, created_at)
            VALUES
                ('evt_old', 'test.event', %s,
                 now() - interval '10 days', now() - interval '10 days'),
                ('evt_recent', 'test.event', %s,
                 now() - interval '2 days', now() - interval '2 days'),
                ('evt_pending', 'test.event', %s,
                 NULL, now() - interval '10 days')
            """,
            (b"bytes1", b"bytes2", b"bytes3"),
        )

        # 2. 插入 enrichment_task_outbox:
        # - task_old: 10 天前已发布
        # - task_recent: 2 天前已发布
        conn.execute(
            """
            INSERT INTO enrichment_task_outbox
                (event_id, task_id, contract_bytes, published_at, created_at)
            VALUES
                ('task_old', 't1', %s,
                 now() - interval '10 days', now() - interval '10 days'),
                ('task_recent', 't2', %s,
                 now() - interval '2 days', now() - interval '2 days')
            """,
            (b"bytes4", b"bytes5"),
        )

        # 执行 7 天安全窗口归档
        result = archive_and_purge_outbox(conn, safety_window_days=7)
        assert result["status"] == "ok"
        assert result["archived_events"] == 1
        assert result["archived_enrichments"] == 1

        # 检查主表残留
        remaining_events = {
            row[0] for row in conn.execute("SELECT event_id FROM event_outbox").fetchall()
        }
        assert remaining_events == {"evt_recent", "evt_pending"}

        remaining_tasks = {
            row[0] for row in conn.execute("SELECT event_id FROM enrichment_task_outbox").fetchall()
        }
        assert remaining_tasks == {"task_recent"}

        # 检查归档表记录
        archived_events = {
            row[0] for row in conn.execute("SELECT event_id FROM event_outbox_archive").fetchall()
        }
        assert archived_events == {"evt_old"}

        archived_tasks = {
            row[0]
            for row in conn.execute(
                "SELECT event_id FROM enrichment_task_outbox_archive"
            ).fetchall()
        }
        assert archived_tasks == {"task_old"}
