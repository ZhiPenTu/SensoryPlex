"""index-worker 落库侧的集成测试：真实 PostgreSQL + 真实迁移。

这一层专治"只有真库能暴露"的缺陷：`records` 的 SQL 里写了不存在的列、或者状态不变式
其实没落进数据库时，纯函数契约测试会全绿而这里会直接报 `UndefinedColumn` / 约束冲突。
向量库一侧不在这里（`tools/verify_index.py` 用真实 Milvus Lite 覆盖）。
"""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_index_worker import records

from tools.migrate import migrate

pytestmark = pytest.mark.integration

OWNER = "index-integration-owner"
OTHER_OWNER = "index-integration-other"
STREAM = "stream_index_integration"
SOURCE = "source_index_integration"
MATERIAL = "material_index_integration"
KEY = "material_text_bge_small_zh_v1_5_d512_v1"
DIGEST = "sha256:" + "d" * 64


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
                "backend,config_hash) VALUES ('model_integration','bge','1.0',%s,'cpu',%s)",
                (DIGEST, DIGEST),
            )
            insert_material(conn, 1)
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def insert_material(conn, revision: int, status: str = "fast_ready") -> None:
    conn.execute(
        "INSERT INTO material_unit(material_unit_id,revision,stream_id,start_ms,end_ms,status,"
        "search_text,contract_bytes,content_hash) VALUES (%s,%s,%s,0,1000,%s,'',%s,%s)",
        (MATERIAL, revision, STREAM, status, b"", DIGEST),
    )


def stage_ready(conn, *, observation_id: str = "obs_index_integration", revision: int = 1) -> str:
    embedding_id = records.embedding_id_for(observation_id, MATERIAL, revision)
    records.begin_pending(
        conn,
        embedding_id=embedding_id,
        material_unit_id=MATERIAL,
        material_revision=revision,
        model_release_id="model_integration",
        observation_id=observation_id,
        vector_index_key=KEY,
        dimension=512,
        content_hash=DIGEST,
    )
    records.mark_ready(conn, embedding_id, vector_ref=f"milvus://{KEY}/{embedding_id}")
    return embedding_id


def test_ready_record_is_confirmed_and_resolvable(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        state = records.record_state(conn, embedding_id)
        assert state["state"] == "ready"
        assert state["vector_ref"] == f"milvus://{KEY}/{embedding_id}"
        assert state["error_code"] is None
        assert state["indexed_at"] is not None

        resolved = records.resolve_ready(conn, OWNER, [embedding_id], limit=5)[embedding_id]
        assert resolved["material_unit_id"] == MATERIAL
        assert resolved["material_revision"] == 1
        assert resolved["stream_id"] == STREAM
        assert (resolved["start_ms"], resolved["end_ms"]) == (0, 1000)
        assert resolved["superseded"] is False
        assert records.ready_ids(conn, OWNER, KEY) == [embedding_id]


def test_newer_revision_marks_the_hit_superseded(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        insert_material(conn, 2)
        resolved = records.resolve_ready(conn, OWNER, [embedding_id], limit=5)[embedding_id]
        assert resolved["superseded"] is True


def test_failed_material_is_never_resolved(database):
    with psycopg.connect(database) as conn:
        # 素材事实不可原地改（0001 的 `deny_fact_update` 触发器），所以"失败的素材"
        # 只能作为新 revision 出现——这里直接把那个 revision 造成 failed。
        insert_material(conn, 2, status="failed")
        embedding_id = stage_ready(conn, observation_id="obs_index_failed_material", revision=2)
        assert records.resolve_ready(conn, OWNER, [embedding_id], limit=5) == {}
        assert records.ready_ids(conn, OWNER, KEY) == []


def test_another_owner_gets_nothing(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        assert records.resolve_ready(conn, OTHER_OWNER, [embedding_id], limit=5) == {}
        assert records.ready_ids(conn, OTHER_OWNER, KEY) == []


def test_failed_state_drops_the_hit_even_though_the_reference_remains(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        records.mark_failed(conn, embedding_id, error_code="vector_store_locked")
        state = records.record_state(conn, embedding_id)
        assert state["state"] == "failed"
        assert state["error_code"] == "vector_store_locked"
        # 最近一次确认写入仍然记着，但 state=failed 的命中不许被返回。
        assert state["vector_ref"] == f"milvus://{KEY}/{embedding_id}"
        assert records.resolve_ready(conn, OWNER, [embedding_id], limit=5) == {}


def test_replayed_identity_is_idempotent_and_conflicts_are_explicit(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        stage_ready(conn)
        assert conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0] == 1
        with pytest.raises(records.IdentityConflict) as failure:
            records.begin_pending(
                conn,
                embedding_id=embedding_id,
                material_unit_id=MATERIAL,
                material_revision=1,
                model_release_id="model_integration",
                observation_id="obs_another_observation",
                vector_index_key=KEY,
                dimension=512,
                content_hash=DIGEST,
            )
        assert str(failure.value) == "embedding_identity_conflict"
        with pytest.raises(records.IdentityConflict) as failure:
            records.begin_pending(
                conn,
                embedding_id=embedding_id,
                material_unit_id=MATERIAL,
                material_revision=1,
                model_release_id="model_integration",
                observation_id="obs_index_integration",
                vector_index_key=KEY,
                dimension=512,
                content_hash="sha256:" + "e" * 64,
            )
        assert str(failure.value) == "embedding_payload_conflict"
        with pytest.raises(records.IdentityConflict) as failure:
            records.mark_ready(conn, "emb_" + "0" * 32, vector_ref="milvus://x/y")
        assert str(failure.value) == "embedding_record_missing"


def test_migration_0003_invariants_are_enforced_by_the_database(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn)
        cases = [
            (
                "embedding_record_ready_is_confirmed",
                "UPDATE embedding_record SET indexed_at=NULL WHERE embedding_id=%s",
            ),
            (
                "embedding_record_ready_has_no_error",
                "UPDATE embedding_record SET error_code='x' WHERE embedding_id=%s",
            ),
            (
                "embedding_record_failed_has_reason",
                "UPDATE embedding_record SET state='failed' WHERE embedding_id=%s",
            ),
        ]
        for constraint, statement in cases:
            with pytest.raises(psycopg.errors.CheckViolation) as failure:
                # 每个用例自己的 savepoint：一条约束冲突不许把上面备好的行一起回滚掉。
                with conn.transaction():
                    conn.execute(statement, (embedding_id,))
            assert failure.value.diag.constraint_name == constraint


def test_reconciliation_identifies_superseded_and_tombstones(database):
    with psycopg.connect(database) as conn:
        embedding_id = stage_ready(conn, revision=1)
        insert_material(conn, revision=2)

        candidates = records.find_reconciliation_candidates(conn, KEY)
        assert len(candidates) == 1
        assert candidates[0]["embedding_id"] == embedding_id
        assert candidates[0]["reason"] == "superseded"

        tombstoned = records.mark_tombstone(conn, [embedding_id])
        assert tombstoned == 1

        state = records.record_state(conn, embedding_id)
        assert state["state"] == "tombstoned"
        assert state["vector_ref"] is None


def test_pgvector_adapter_and_reconcile_cycle(database):
    from sensoryplex_index_worker.milvus_store import VectorIndex
    from sensoryplex_index_worker.reconciliation import reconcile_cycle

    with psycopg.connect(database) as conn:
        index = VectorIndex(database, KEY, engine="pgvector")
        index.ensure_collection()
        assert index.is_shared is True
        assert index.engine_name == "pgvector"

        embedding_id = stage_ready(conn, revision=1)
        vector = [0.1] * 512
        inserted = index.upsert(
            [
                {
                    "embedding_id": embedding_id,
                    "material_unit_id": MATERIAL,
                    "material_revision": 1,
                    "stream_id": STREAM,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "modality": "text_embedding",
                    "model_release_id": "model_integration",
                    "observation_id": "obs_index_integration",
                    "content_hash": DIGEST,
                    "created_at_unix_ms": 1700000000000,
                    "vector": vector,
                }
            ]
        )
        assert inserted == 1
        assert index.count() == 1

        search_hits = index.search(vector, limit=5)
        assert len(search_hits) == 1
        assert search_hits[0]["embedding_id"] == embedding_id

        # 推进新版本，触发废弃
        insert_material(conn, revision=2)

        # 执行对账与物理 GC
        outcome = reconcile_cycle(conn, index, batch=10, dry_run=False)
        assert outcome["candidates_found"] == 1
        assert outcome["vectors_deleted"] == 1
        assert outcome["tombstoned_count"] == 1
        assert index.count() == 0

        state = records.record_state(conn, embedding_id)
        assert state["state"] == "tombstoned"
        index.close()
