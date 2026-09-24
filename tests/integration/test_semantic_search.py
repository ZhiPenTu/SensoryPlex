"""网关语义检索接线：API ↔ 检索面的**真实 gRPC 往返** + 真实 PostgreSQL 事实水合（ADR-023）。

这一层专治"只有真链路能暴露"的缺陷：proto 字段名对不上、gRPC metadata 没带对、原因码在
最后一跳被改写、水合不到素材时结果集静默变小——纯函数契约测试都会全绿，这里会直接红。

上游检索面在本文件里是**进程内真实 gRPC 服务 + 测试替身实现**：它只回答"哪些 embedding
命中"，不含向量库与 BGE 编码器。真实编码与真实 Milvus 由 `tools/verify_semantic_search.py`
承担，因此本文件通过**不等于**语义检索可用。
"""

import contextlib
import os
import socket
import uuid
from concurrent import futures

import grpc
import psycopg
import pytest
from edge_material_sdk.generated.index.v1 import index_pb2, index_pb2_grpc
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_api.app import create_app
from sensoryplex_api.infrastructure.materials import append_material
from sensoryplex_api.settings import Settings

from tools.migrate import migrate

pytestmark = pytest.mark.integration

TOKEN = "contract-test-token-not-a-deployment-secret"
SURFACE_TOKEN = "index-surface-token-that-is-long-enough"
KEY = "material_text_bge_small_zh_v1_5_d512_v1"
RELEASE = "bge:bge-small-zh-v1.5@0123456789ab"


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


def seed(conn, material, *, owner: str = "owner") -> None:
    """仅供测试使用的引用数据，不是模拟的 ingestion/AI 结果。"""
    observation = material.observations[0]
    provenance = observation.provenance
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) VALUES (%s,'file',%s,%s)",
        (observation.source_id, "private://not-exposed", owner),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped')",
        (observation.stream_id, observation.source_id),
    )
    conn.execute(
        "INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms) "
        "VALUES (%s,%s,'private://not-exposed',%s,'contract-only',1000)",
        (material.source_refs[0].asset_id, observation.stream_id, observation.content_hash),
    )
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            provenance.model_release_id,
            provenance.model_id,
            provenance.model_version,
            provenance.model_artifact_digest,
            provenance.execution_backend,
            provenance.config_hash,
        ),
    )
    conn.execute(
        "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) VALUES (%s,%s,%s,%s,%s)",
        (observation.source_item_id, observation.stream_id, "audio_segment", 100, 200),
    )
    assert append_material(conn, material, trace_id="semantic-search-contract")


def hit(unit: str, revision: int = 1, distance: float = 0.12) -> index_pb2.SemanticHit:
    return index_pb2.SemanticHit(
        embedding_id="emb_" + "0" * 32,
        material_unit_id=unit,
        material_revision=revision,
        distance=distance,
        vector_ref=f"milvus://{KEY}/emb_" + "0" * 32,
        observation_id="obs_contract",
        model_release_id=RELEASE,
        dimension=512,
    )


class RecordingServicer(index_pb2_grpc.IndexSearchServiceServicer):
    """测试替身：记录 API 真正发了什么，按脚本回答，不做任何"看起来像检索"的推断。"""

    def __init__(self, *, hits=(), unindexed_hits=0, error=None):
        self.hits = list(hits)
        self.unindexed_hits = unindexed_hits
        self.error = error
        self.calls: list[index_pb2.SemanticSearchRequest] = []
        self.tokens: list[str] = []

    def SearchSemantic(self, request, context):
        self.calls.append(request)
        metadata = {item.key: item.value for item in context.invocation_metadata()}
        self.tokens.append(metadata.get("authorization", ""))
        if metadata.get("authorization") != f"Bearer {SURFACE_TOKEN}":
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "index_auth_failed")
        if self.error is not None:
            return index_pb2.SemanticSearchResponse(error=index_pb2.IndexFailure(**self.error))
        return index_pb2.SemanticSearchResponse(
            hits=self.hits,
            vector_index_key=KEY,
            collection=KEY,
            index_version="milvus-flat-cosine-v1",
            unindexed_hits=self.unindexed_hits,
            query_model_release_id=RELEASE,
            query_dimension=512,
        )


@contextlib.contextmanager
def surface(servicer):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    index_pb2_grpc.add_IndexSearchServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port, "the in-process index surface did not bind"
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(None)


def settings_for(database, endpoint: str) -> Settings:
    return Settings(
        database_url=database,
        api_token=TOKEN,
        principal="owner",
        index_search_endpoint=endpoint,
        index_search_token=SURFACE_TOKEN,
    )


def test_semantic_search_hydrates_real_facts_and_reports_the_distance(database, material):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    servicer = RecordingServicer(hits=[hit("material_contract", distance=0.12)])
    with surface(servicer) as endpoint:
        with TestClient(create_app(settings_for(database, endpoint))) as client:
            response = client.post(
                "/v1/materials:search",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"mode": "semantic", "query": "季度销售"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["mode"] == "semantic"
            assert body["index_version"] == "milvus-flat-cosine-v1"
            assert body["vector_index_key"] == KEY
            assert body["unindexed_hits"] == 0 and body["unresolved_hits"] == 0
            assert [item["material_unit_id"] for item in body["materials"]] == ["material_contract"]
            assert [item["distance"] for item in body["hits"]] == [0.12]
            assert body["hits"][0]["material_unit_id"] == "material_contract"
            assert body["hits"][0]["vector_ref"] == f"milvus://{KEY}/emb_" + "0" * 32
            assert "private://" not in response.text
    sent = servicer.calls[0]
    assert (sent.query, sent.principal, sent.vector_index_key, sent.limit) == (
        "季度销售",
        "owner",
        KEY,
        20,
    )
    assert servicer.tokens == [f"Bearer {SURFACE_TOKEN}"]


def test_discarded_hits_and_unhydrated_hits_are_counted_separately(database, material):
    """worker 丢掉的命中（越权/作废）与"返回前素材已不可读"是两件事，两个数都要看得见。"""
    with psycopg.connect(database) as conn:
        seed(conn, material)
    servicer = RecordingServicer(
        hits=[hit("material_contract"), hit("material_vanished", distance=0.3)],
        unindexed_hits=2,
    )
    with surface(servicer) as endpoint:
        with TestClient(create_app(settings_for(database, endpoint))) as client:
            body = client.post(
                "/v1/materials:search",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"mode": "semantic", "query": "销售", "limit": 3},
            ).json()
    assert len(body["materials"]) == 1
    assert len(body["hits"]) == 1
    assert body["unindexed_hits"] == 2
    assert body["unresolved_hits"] == 1
    assert servicer.calls[0].limit == 3


@pytest.mark.parametrize(
    ("error", "status", "retryable"),
    [
        ({"reason_code": "vector_store_locked", "retryable": True}, 503, True),
        ({"reason_code": "vector_store_unavailable", "retryable": True}, 503, True),
        ({"reason_code": "vector_collection_contract_mismatch"}, 502, False),
        ({"reason_code": "query_model_release_mismatch"}, 502, False),
    ],
)
def test_surface_reason_codes_reach_the_client_verbatim(
    database, material, error, status, retryable
):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    with surface(RecordingServicer(error=error)) as endpoint:
        with TestClient(create_app(settings_for(database, endpoint))) as client:
            response = client.post(
                "/v1/materials:search",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"mode": "semantic", "query": "销售"},
            )
    assert response.status_code == status, response.text
    assert response.json()["reason_code"] == error["reason_code"]
    assert response.json()["retryable"] is retryable


def test_wrong_surface_token_is_a_configuration_error_not_a_retry(database, material):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    settings = settings_for(database, "127.0.0.1:1")
    settings.index_search_token = type(settings.index_search_token)("x" * 40)
    with surface(RecordingServicer()) as endpoint:
        settings.index_search_endpoint = endpoint
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/materials:search",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"mode": "semantic", "query": "销售"},
            )
    assert response.status_code == 503, response.text
    assert response.json()["reason_code"] == "semantic_index_unauthenticated"
    assert response.json()["retryable"] is False


def test_unreachable_surface_is_retryable_and_never_looks_like_no_results(database, material):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    with TestClient(create_app(settings_for(database, f"127.0.0.1:{closed}"))) as client:
        response = client.post(
            "/v1/materials:search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"mode": "semantic", "query": "销售"},
        )
    assert response.status_code == 503, response.text
    assert response.json()["reason_code"] == "semantic_index_unreachable"
    assert response.json()["retryable"] is True
    assert "materials" not in response.json()


def test_semantic_rejects_what_it_does_not_implement_without_calling_the_surface(
    database, material
):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    servicer = RecordingServicer(hits=[hit("material_contract")])
    with surface(servicer) as endpoint:
        with TestClient(create_app(settings_for(database, endpoint))) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            for body, status, reason in (
                ({"mode": "semantic"}, 422, "invalid_query"),
                (
                    {"mode": "semantic", "query": "x", "stream_id": "stream_contract"},
                    422,
                    "semantic_filters_not_supported",
                ),
                (
                    {"mode": "semantic", "query": "x", "modalities": ["ocr_block"]},
                    422,
                    "semantic_filters_not_supported",
                ),
                (
                    {"mode": "semantic", "query": "x", "tags": ["财务"]},
                    422,
                    "semantic_filters_not_supported",
                ),
                (
                    {"mode": "semantic", "query": "x", "start_ms": 1},
                    422,
                    "semantic_filters_not_supported",
                ),
                (
                    {"mode": "semantic", "query": "x", "min_confidence": 0.5},
                    422,
                    "semantic_filters_not_supported",
                ),
                ({"mode": "adjacent", "query": "x"}, 422, "invalid_search_mode"),
            ):
                response = client.post("/v1/materials:search", headers=headers, json=body)
                assert response.status_code == status, (body, response.text)
                assert response.json()["detail"] == reason, (body, response.text)
    assert servicer.calls == []


def test_unconfigured_semantic_search_reports_unavailable_not_not_implemented(
    database, material, bare_settings
):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    # 本用例断言"检索面未配置时是显式不可用"，所以环境不许替它决定（见 tests/conftest.py）。
    settings = bare_settings(database_url=database, api_token=TOKEN, principal="owner")
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        response = client.post(
            "/v1/materials:search", headers=headers, json={"mode": "semantic", "query": "销售"}
        )
        assert response.status_code == 503, response.text
        assert response.json()["reason_code"] == "semantic_search_unavailable"
        assert response.json()["retryable"] is False
        capabilities = client.get("/v1/capabilities", headers=headers).json()["capabilities"]
        semantic = next(item for item in capabilities if item["name"] == "semantic_search")
        assert semantic["available"] is False
        assert semantic["reason"] == "semantic_search_unavailable"
        assert client.get("/v1/health").json()["capabilities"]["semantic_search"] is False


def test_configured_semantic_search_is_reported_available_without_claiming_it_works(
    database, material
):
    with psycopg.connect(database) as conn:
        seed(conn, material)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    with TestClient(create_app(settings_for(database, f"127.0.0.1:{closed}"))) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        capabilities = client.get("/v1/capabilities", headers=headers).json()["capabilities"]
        semantic = next(item for item in capabilities if item["name"] == "semantic_search")
        assert semantic["available"] is True and semantic["reason"] == ""
        assert client.get("/v1/health").json()["capabilities"]["semantic_search"] is True


def test_keyword_mode_never_reports_semantic_fields(database, material):
    """keyword 不排名：hits 必须是空的，而不是补一串 0 距离。"""
    with psycopg.connect(database) as conn:
        seed(conn, material)
    settings = Settings(database_url=database, api_token=TOKEN, principal="owner")
    with TestClient(create_app(settings)) as client:
        body = client.post(
            "/v1/materials:search",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"query": "销售"},
        ).json()
    assert body["mode"] == "keyword"
    assert body["index_version"] == "postgres-literal-v1"
    assert body["hits"] == []
    assert body["vector_index_key"] == ""
    assert body["unindexed_hits"] == 0 and body["unresolved_hits"] == 0
    assert len(body["materials"]) == 1
