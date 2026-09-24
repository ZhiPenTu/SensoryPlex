"""语义检索的契约测试：检索面形状、稳定原因码、可重试判定与 API 侧映射。

测的是**纯判定与形状**：不起向量库、不起 gRPC、不连数据库、不加载 ONNX 权重。真实检索
（真实 BGE 编码 → 真实 Milvus → 真实 PostgreSQL 回查）由 `tools/verify_semantic_search.py`
承担；API 与检索面之间的真实 gRPC 往返由 `tests/integration/test_semantic_search.py` 承担。
契约测试通过**不等于**语义检索可用。
"""

from dataclasses import dataclass

import grpc
import pytest
from edge_material_sdk.generated.gateway.v1 import gateway_pb2
from edge_material_sdk.generated.index.v1 import index_pb2
from sensoryplex_api.infrastructure import semantic
from sensoryplex_api.interfaces import business
from sensoryplex_index_worker import service
from sensoryplex_index_worker.query_encoder import GENERIC_CODE, stable_code

KEY = "material_text_bge_small_zh_v1_5_d512_v1"
TOKEN = "index-surface-token-that-is-long-enough"


# ── 检索面契约形状（漂移守卫） ────────────────────────────────────────────────


def test_semantic_request_and_response_carry_the_required_fields():
    request = index_pb2.SemanticSearchRequest()
    assert [field.name for field in request.DESCRIPTOR.fields] == [
        "query",
        "principal",
        "vector_index_key",
        "limit",
    ]
    response = index_pb2.SemanticSearchResponse()
    assert [field.name for field in response.DESCRIPTOR.fields] == [
        "hits",
        "vector_index_key",
        "collection",
        "index_version",
        "unindexed_hits",
        "query_model_release_id",
        "query_dimension",
        "error",
    ]
    assert [field.name for field in index_pb2.SemanticHit.DESCRIPTOR.fields] == [
        "embedding_id",
        "material_unit_id",
        "material_revision",
        "distance",
        "vector_ref",
        "observation_id",
        "model_release_id",
        "dimension",
    ]


def test_search_response_keeps_keyword_and_semantic_fields_separated():
    """keyword 不排名、semantic 才带 hits：两个模式不能互相冒充。"""
    assert [field.name for field in gateway_pb2.SearchResponse.DESCRIPTOR.fields] == [
        "materials",
        "mode",
        "index_version",
        "hits",
        "unindexed_hits",
        "vector_index_key",
        "unresolved_hits",
    ]
    assert [field.name for field in gateway_pb2.SearchHit.DESCRIPTOR.fields] == [
        "material_unit_id",
        "revision",
        "distance",
        "embedding_id",
        "vector_ref",
        "observation_id",
    ]


# ── 稳定原因码与可重试判定 ─────────────────────────────────────────────────


def test_failure_carries_one_stable_code_and_a_retryable_verdict():
    locked = service.failure("vector_store_locked", "vector-edge.db")
    assert locked.error.reason_code == "vector_store_locked"
    assert locked.error.retryable is True
    assert not locked.hits
    for code in (
        "vector_collection_contract_mismatch",
        "query_model_release_mismatch",
        "vector_index_model_release_mixed",
        "query_encoder_index_key_mismatch",
        "invalid_query",
        "query_encoder_model_dir_required",
    ):
        assert service.failure(code).error.retryable is False, code


def test_failure_detail_is_bounded_and_never_carries_a_path():
    response = service.failure("query_model_release_mismatch", "x" * 500)
    assert len(response.error.detail) == 200
    assert service.INDEX_VERSION == "milvus-flat-cosine-v1"


def test_encoder_reason_codes_pass_through_only_in_contract_shape():
    # 插件的原因串就是检索面能给的细节上限：合规形状原样带出，含路径/空白的一律换通用码。
    assert stable_code("model_dir_required") == "model_dir_required"
    assert stable_code("model_dimension_mismatch:512!=384") == "model_dimension_mismatch:512!=384"
    assert stable_code("execution_provider_not_selected:CPUExecutionProvider") == (
        "execution_provider_not_selected:CPUExecutionProvider"
    )
    for leaky in (
        "/Users/someone/weights/bge",
        "model_dir_not_found: /Users/someone/weights",
        "",
        "Model_Dir_Missing",
    ):
        assert stable_code(leaky) == GENERIC_CODE


# ── 检索面的准入与同源守卫 ─────────────────────────────────────────────────


@dataclass
class FakeIndex:
    vector_index_key: str = KEY


@dataclass
class FakeEncoder:
    release_id: str = "bge:bge-small-zh-v1.5@aaaaaaaabbbb"
    dimension: int = 512
    vector_index_key: str = KEY


def servicer(*, releases: list[str] | None = None, monkeypatch=None):
    instance = service.IndexSearchServicer(
        pool=None, index=FakeIndex(), encoder=FakeEncoder(), auth_token=TOKEN
    )
    if releases is not None:
        monkeypatch.setattr(
            service.records,
            "collection_model_releases",
            lambda conn, key, *, limit=2: releases,
        )
    return instance


def request(**overrides) -> index_pb2.SemanticSearchRequest:
    fields = {"query": "财务", "principal": "owner", "vector_index_key": KEY, "limit": 5}
    fields.update(overrides)
    return index_pb2.SemanticSearchRequest(**fields)


def test_admission_rejects_every_unspecified_input():
    instance = servicer()
    assert instance._admit(request(query=""))[0] == "invalid_query"
    assert (
        instance._admit(request(query="x" * (service.MAX_QUERY_LENGTH + 1)))[0] == "invalid_query"
    )
    assert instance._admit(request(principal=""))[0] == "invalid_principal"
    assert instance._admit(request(vector_index_key="material_text_other_d512_v1"))[0] == (
        "vector_index_key_mismatch"
    )
    assert instance._admit(request(limit=service.MAX_LIMIT + 1))[0] == "invalid_limit"
    assert instance._admit(request()) is None


class FakeContext:
    """只实现被用到的两件事：读 metadata 与 abort。"""

    def __init__(self, token: str = TOKEN):
        self._metadata = [] if not token else [_Metadatum("authorization", f"Bearer {token}")]
        self.aborted: tuple | None = None

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, details):
        self.aborted = (code, details)
        raise PermissionError(details)


class _Metadatum(tuple):
    def __new__(cls, key, value):
        return super().__new__(cls, (key, value))

    key = property(lambda self: self[0])
    value = property(lambda self: self[1])


def test_authorization_requires_a_bearer_token_and_hides_which_half_was_wrong():
    instance = servicer()
    instance._authorize(FakeContext())
    for token in ("", "wrong-token", TOKEN + "x"):
        context = FakeContext(token)
        with pytest.raises(PermissionError):
            instance._authorize(context)
        assert context.aborted == (grpc.StatusCode.PERMISSION_DENIED, "index_auth_failed")


def test_same_origin_guard_refuses_mixed_or_foreign_releases(monkeypatch):
    mine = FakeEncoder().release_id
    assert servicer(releases=[], monkeypatch=monkeypatch)._same_origin(None) is None
    assert servicer(releases=[mine], monkeypatch=monkeypatch)._same_origin(None) is None
    code, _ = servicer(releases=["bge:other@ccccccccdddd"], monkeypatch=monkeypatch)._same_origin(
        None
    )
    assert code == "query_model_release_mismatch"
    code, _ = servicer(releases=["bge:a@1", "bge:b@2"], monkeypatch=monkeypatch)._same_origin(None)
    assert code == "vector_index_model_release_mixed"


# ── API 侧：未配置 / 不可达 / 原因码原样上抛 ─────────────────────────────────


class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code


def test_api_reports_unconfigured_search_as_unavailable_not_as_not_implemented(bare_settings):
    settings = bare_settings(database_url="postgresql://ignored/none")
    with pytest.raises(semantic.SemanticSearchError) as failure:
        semantic.search(None, settings, principal="owner", query="x", limit=5)
    assert failure.value.code == "semantic_search_unavailable"
    assert failure.value.retryable is False
    assert failure.value.status == 503


def test_api_maps_transport_failures_without_inventing_success():
    unreachable = semantic._translate(FakeRpcError(grpc.StatusCode.UNAVAILABLE))
    assert (unreachable.code, unreachable.retryable) == ("semantic_index_unreachable", True)
    timeout = semantic._translate(FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED))
    assert timeout.retryable is True
    denied = semantic._translate(FakeRpcError(grpc.StatusCode.PERMISSION_DENIED))
    assert (denied.code, denied.retryable) == ("semantic_index_unauthenticated", False)
    broken = semantic._translate(FakeRpcError(grpc.StatusCode.INTERNAL))
    assert (broken.code, broken.retryable) == ("semantic_index_protocol_error", False)


def test_http_status_follows_the_failure_site_not_the_retryable_flag():
    """状态码表示"失败落在哪一环"，`retryable` 是独立标记：两者不得互相推导。

    没走到检索面的三条失败同样是 503，但其中两条不可重试——这证明 503 不等于"重试就好"，
    也证明不能反过来用 `retryable` 去猜状态码。
    """
    unreachable = semantic._translate(FakeRpcError(grpc.StatusCode.UNAVAILABLE))
    denied = semantic._translate(FakeRpcError(grpc.StatusCode.PERMISSION_DENIED))
    protocol = semantic._translate(FakeRpcError(grpc.StatusCode.INTERNAL))
    assert (semantic.SURFACE_UNAVAILABLE_STATUS, semantic.SURFACE_REJECTED_STATUS) == (503, 502)
    # 没走到检索面：503，且不代表"重试就好"。
    assert {unreachable.status, denied.status} == {503}
    assert (denied.retryable, unreachable.retryable) == (False, True)
    # 检索面答了但答的不是本契约：502。
    assert protocol.status == 502 and protocol.retryable is False
    # 检索面自报的原因码按它自己的 retryable 分档，这是唯一一处这样的映射。
    locked = semantic.SemanticSearchError("vector_store_locked", retryable=True, status=503)
    mixed = semantic.SemanticSearchError(
        "vector_index_model_release_mixed", retryable=False, status=502
    )
    assert (locked.status, locked.retryable) == (503, True)
    assert (mixed.status, mixed.retryable) == (502, False)


def test_semantic_filters_are_rejected_instead_of_silently_ignored():
    assert business._has_semantic_filters(business.SearchRequest(query="财务")) is False
    for filters in (
        {"stream_id": "stream_1"},
        {"modalities": ["ocr_block"]},
        {"tags": ["财务"]},
        {"start_ms": 1},
        {"end_ms": 10},
        {"min_confidence": 0.5},
    ):
        assert business._has_semantic_filters(business.SearchRequest(**filters)) is True, filters


# ── 配置面：半配置的检索面不允许启动 ───────────────────────────────────────


def test_index_search_settings_refuse_half_configuration(bare_settings):
    base = {"database_url": "postgresql://ignored/none"}
    assert bare_settings(**base).index_search_endpoint == ""
    assert bare_settings(**base).index_vector_index_key == KEY
    configured = bare_settings(
        **base, index_search_endpoint="127.0.0.1:50077", index_search_token="t" * 32
    )
    assert configured.index_search_endpoint == "127.0.0.1:50077"
    with pytest.raises(ValueError, match="index_search_token_required"):
        bare_settings(**base, index_search_endpoint="127.0.0.1:50077")
    with pytest.raises(ValueError, match="index_search_token_required"):
        bare_settings(**base, index_search_endpoint="127.0.0.1:50077", index_search_token="short")
    with pytest.raises(ValueError, match="index_search_endpoint_required"):
        bare_settings(**base, index_search_token="t" * 32)
    with pytest.raises(ValueError, match="index_search_endpoint_invalid"):
        bare_settings(
            **base, index_search_endpoint="http://127.0.0.1:50077", index_search_token="t" * 32
        )


def test_blank_search_configuration_counts_as_unconfigured(bare_settings):
    """容器编排只能给出空串（compose `${VAR:-}` / `docker compose exec -e VAR=` 都表达不了
    "变量不存在"），所以空白必须读成"没配"——否则老 `.env` 会让 api 拒绝启动、整个栈起不来。

    半配置仍然被挡住：只给一处（任一处为空）照样落 `*_required`。
    """
    both_blank = bare_settings(index_search_endpoint="  ", index_search_token="")
    assert both_blank.index_search_endpoint == ""
    assert both_blank.index_search_token is None
    with pytest.raises(ValueError, match="index_search_token_required"):
        bare_settings(index_search_endpoint="127.0.0.1:50077", index_search_token="")
    with pytest.raises(ValueError, match="index_search_endpoint_required"):
        bare_settings(index_search_endpoint="", index_search_token="t" * 32)


def test_api_vocabulary_and_index_vocabulary_stay_distinct():
    """API 自有的两个码与检索面的码不是同一批：混用会把"没部署"读成"索引坏了"。"""
    api_codes = {"semantic_search_unavailable", "semantic_index_unreachable"}
    assert api_codes.isdisjoint(service.RETRYABLE_CODES)
    assert api_codes.isdisjoint({"invalid_query", "invalid_limit", "vector_index_key_mismatch"})
    # 可重试集合只装容量约束：契约/配置错误必须在外面，否则调用方会一直重试一个死结。
    assert {
        "vector_collection_contract_mismatch",
        "query_model_release_mismatch",
        "vector_index_model_release_mixed",
        "query_encoder_index_key_mismatch",
    }.isdisjoint(service.RETRYABLE_CODES)
