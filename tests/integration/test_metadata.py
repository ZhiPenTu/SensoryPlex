import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_gateway.app import create_app
from sensoryplex_gateway.repository import (
    RevisionConflict,
    append_material,
    get_material,
    get_material_lineage,
)
from sensoryplex_gateway.settings import Settings

from tools.migrate import migrate

pytestmark = pytest.mark.integration
TOKEN = "contract-test-token-not-a-deployment-secret"


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
        migrate(isolated)
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def seed_references(conn, material):
    """仅供测试使用的引用数据，不是模拟的 ingestion/AI 结果。"""
    obs = material.observations[0]
    p = obs.provenance
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) VALUES (%s,%s,%s,%s)",
        (obs.source_id, "file", "[redacted]", "owner"),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped')",
        (obs.stream_id, obs.source_id),
    )
    conn.execute(
        "INSERT INTO media_asset VALUES (%s,%s,%s,%s,%s,%s)",
        (
            "asset_contract",
            obs.stream_id,
            "private://not-exposed",
            obs.content_hash,
            "contract-only",
            1000,
        ),
    )
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            p.model_release_id,
            p.model_id,
            p.model_version,
            p.model_artifact_digest,
            p.execution_backend,
            p.config_hash,
        ),
    )
    conn.execute(
        "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) VALUES (%s,%s,%s,%s,%s)",
        (obs.source_item_id, obs.stream_id, "audio_segment", 100, 200),
    )


def test_append_replay_conflict_history_and_atomic_outbox(database, material):
    with psycopg.connect(database) as conn:
        seed_references(conn, material)
        assert append_material(conn, material, trace_id="contract-trace")
        assert not append_material(conn, material, trace_id="contract-trace")
        assert conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 1
        material.tags.append("changed")
        with pytest.raises(RevisionConflict):
            append_material(conn, material, trace_id="contract-trace")
        material.revision = 2
        assert append_material(conn, material, trace_id="contract-trace")
        assert get_material(conn, "owner", material.material_unit_id).revision == 2
        old = get_material(conn, "owner", material.material_unit_id, 1)
        assert old.superseded and "changed" not in old.tags
        assert get_material(conn, "intruder", material.material_unit_id) is None
        material.revision = 3
        material.observations[0].payload.update({"text": "mutated immutable observation"})
        with pytest.raises(RevisionConflict, match="observation"):
            append_material(conn, material, trace_id="contract-trace")
        assert conn.execute("SELECT count(*) FROM material_unit").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 2


def test_api_filters_auth_time_boundaries_and_unavailable_capabilities(
    database, material, bare_settings
):
    with psycopg.connect(database) as conn:
        seed_references(conn, material)
        append_material(conn, material, trace_id="contract-trace")
    # 本用例断言"检索面未配置时是显式不可用"，所以环境不许替它决定（见 tests/conftest.py）。
    settings = bare_settings(database_url=database, api_token=TOKEN, principal="owner")
    # 测试 schema 必须在 pool 建立后仍可用：连接选项中包含 search_path。
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/v1/health").status_code == 200
        assert client.post("/v1/materials:search", json={}).status_code == 401
        result = client.post(
            "/v1/materials:search",
            headers=headers,
            json={"query": "销售", "tags": ["财务"], "min_confidence": 0.8},
        )
        assert result.status_code == 200, result.text
        assert len(result.json()["materials"]) == 1
        assert "private://" not in result.text
        for query in (
            {"start_ms": 200},
            {"end_ms": 100},
            {"query": "_"},
            {"modalities": ["ocr_block"]},
            {"min_confidence": 0.99},
        ):
            response = client.post("/v1/materials:search", headers=headers, json=query)
            assert response.status_code == 200
            assert response.json()["materials"] == []
        for query in ({"limit": 101}, {"start_ms": -1}, {"end_ms": 0}, {"unknown": True}):
            assert (
                client.post("/v1/materials:search", headers=headers, json=query).status_code == 422
            )
        # semantic 不再是 501（ADR-023）：未配置检索面时是"显式不可用"，空查询是输入错误。
        unconfigured = client.post(
            "/v1/materials:search", headers=headers, json={"mode": "semantic", "query": "销售"}
        )
        assert unconfigured.status_code == 503
        assert unconfigured.json()["reason_code"] == "semantic_search_unavailable"
        assert (
            client.post(
                "/v1/materials:search", headers=headers, json={"mode": "semantic"}
            ).status_code
            == 422
        )
        assert client.post("/v1/streams", headers=headers).status_code == 501
        assert client.get("/v1/materials/material_contract", headers=headers).status_code == 200
    with TestClient(
        create_app(Settings(database_url=database, api_token=TOKEN, principal="intruder"))
    ) as client:
        assert (
            client.post("/v1/materials:search", headers=headers, json={}).json()["materials"] == []
        )
        assert client.get("/v1/materials/material_contract", headers=headers).status_code == 404
        assert client.get("/v1/streams/stream_contract", headers=headers).status_code == 404


def test_invalid_lineage_rolls_back(database, material):
    with psycopg.connect(database) as conn:
        seed_references(conn, material)
        material.source_refs[0].content_hash = "sha256:" + "b" * 64
        with pytest.raises(ValueError, match="asset_reference"):
            append_material(conn, material, trace_id="contract-trace")
        assert conn.execute("SELECT count(*) FROM material_unit").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 0


def test_material_revision_forwarding_and_lineage(database, material, bare_settings):
    with psycopg.connect(database) as conn:
        seed_references(conn, material)

        # 1. 初始版本 revision=1
        material.revision = 1
        assert append_material(conn, material, trace_id="trace-rev1")
        assert not append_material(conn, material, trace_id="trace-rev1")

        m1 = get_material(conn, "owner", material.material_unit_id)
        assert m1.revision == 1
        assert not m1.HasField("prev_revision")
        assert not m1.superseded

        # 2. 默认模式 (auto_forward=False): 同一 revision 修改内容直接显式报错
        material.tags.append("version2_tag")
        with pytest.raises(RevisionConflict, match="immutable_revision_conflict"):
            append_material(conn, material, trace_id="trace-fail", auto_forward=False)

        # 3. 启用 auto_forward=True: 自动自增至 revision=2，并设置 prev_revision=1
        appended = append_material(conn, material, trace_id="trace-rev2", auto_forward=True)
        assert appended
        assert material.revision == 2
        assert material.prev_revision == 1

        # 验证最新激活版本
        m2 = get_material(conn, "owner", material.material_unit_id)
        assert m2.revision == 2
        assert m2.prev_revision == 1
        assert not m2.superseded
        assert "version2_tag" in m2.tags

        # 验证历史快照不可变
        m1_hist = get_material(conn, "owner", material.material_unit_id, revision=1)
        assert m1_hist.revision == 1
        assert not m1_hist.HasField("prev_revision")
        assert m1_hist.superseded
        assert "version2_tag" not in m1_hist.tags

        # 4. 幂等重放 (Idempotent Replay): 内容相同时 auto_forward=True 返回 False，不推进版本
        replayed = append_material(conn, material, trace_id="trace-replay", auto_forward=True)
        assert not replayed
        assert material.revision == 2

        # 5. 二次修改内容并 auto_forward: 自动自增至 revision=3，prev_revision=2
        material.tags.append("version3_tag")
        appended_3 = append_material(conn, material, trace_id="trace-rev3", auto_forward=True)
        assert appended_3
        assert material.revision == 3
        assert material.prev_revision == 2

        # 6. 查询完整版本血缘链
        lineage = get_material_lineage(conn, "owner", material.material_unit_id)
        assert len(lineage) == 3
        assert [item["revision"] for item in lineage] == [1, 2, 3]
        assert [item["prev_revision"] for item in lineage] == [None, 1, 2]
        assert [item["superseded"] for item in lineage] == [True, True, False]
        assert "version3_tag" in lineage[2]["tags"]
        assert "version2_tag" in lineage[1]["tags"]
        assert "version2_tag" not in lineage[0]["tags"]

    # 7. 通过 REST API 验证 GET /v1/materials/{key}/lineage 业务端点
    settings = bare_settings(database_url=database, api_token=TOKEN, principal="owner")
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        resp = client.get(f"/v1/materials/{material.material_unit_id}/lineage", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["material_unit_id"] == material.material_unit_id
        assert len(data["lineage"]) == 3
        assert data["lineage"][0]["revision"] == 1
        assert data["lineage"][0]["prev_revision"] is None
        assert data["lineage"][0]["superseded"] is True
        assert data["lineage"][1]["revision"] == 2
        assert data["lineage"][1]["prev_revision"] == 1
        assert data["lineage"][1]["superseded"] is True
        assert data["lineage"][2]["revision"] == 3
        assert data["lineage"][2]["prev_revision"] == 2
        assert data["lineage"][2]["superseded"] is False

        # 不存在的素材返回 404
        not_found = client.get("/v1/materials/non_existent_key/lineage", headers=headers)
        assert not_found.status_code == 404

    # 非授权 principal 无法访问返回 404
    intruder_settings = Settings(database_url=database, api_token=TOKEN, principal="intruder")
    with TestClient(create_app(intruder_settings)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        url = f"/v1/materials/{material.material_unit_id}/lineage"
        assert client.get(url, headers=headers).status_code == 404
