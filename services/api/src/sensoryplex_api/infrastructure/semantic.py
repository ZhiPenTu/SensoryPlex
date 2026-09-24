"""语义检索 adapter：API 只做"鉴权后的转发 + 事实水合"（ADR-023）。

API **不**打开向量库：Milvus Lite 的数据目录是进程独占的（ADR-020 §7），持有索引的是常驻
index-worker 的检索面。于是这里只有三件事：

1. 用配置里的终结点 + 共享令牌调用检索面，超时与失败都有稳定原因码；
2. 把 worker 的**稳定原因码原样上抛**——最后一跳改写原因码正是 ADR-020 §8 记录过的缺陷，
   这里不再重犯；调用方按 `retryable` 区分"稍后重试"与"配置/契约错了"；
3. 按 `(material_unit_id, revision)` 重新水合素材事实。事实水合只有这一处实现，水合不出来
   的命中单独计入 `unresolved_hits`，结果集不静默变小。

未配置、不可达、契约错误是**三件不同的事**：分别是 `semantic_search_unavailable`、
`semantic_index_unreachable`、以及 worker 报回来的那个码。任何一件都不得伪装成"没有命中"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import grpc
from edge_material_sdk.generated.index.v1 import index_pb2, index_pb2_grpc
from edge_material_sdk.generated.material.v1.material_pb2 import MaterialUnit

from . import materials

# 检索面返回的是引用与距离，不是素材本体；上限与检索面同口径（4 MiB）。
MAX_MESSAGE_BYTES = 4 << 20
UNAVAILABLE_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)
UNAUTHENTICATED_CODES = frozenset(
    {grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.UNAUTHENTICATED}
)
# HTTP 状态码只由**失败发生在哪一环**决定，这里定是因为只有这一层看得见这个位置：
# - 压根没走到检索面（未配置 / 连不上 / 令牌不符）：网关自己提供不了该能力 → 503；
# - 检索面答了，但答案是"这事没救"（契约不符 / 模型同源不符）：上游明确拒绝 → 502。
# `retryable` 是与之**独立**的标记——503 也可能是不可重试的（令牌配错了），
# 所以状态码不得被读作重试语义，`retryable` 也不得被读成"状态码会再变一次"。
SURFACE_UNAVAILABLE_STATUS = 503
SURFACE_REJECTED_STATUS = 502


class SemanticSearchError(Exception):
    """带稳定原因码的语义检索失败；`retryable` 决定调用方该重试还是该改配置。"""

    def __init__(self, code: str, *, retryable: bool, status: int):
        self.code = code
        self.retryable = retryable
        self.status = status
        super().__init__(code)


@dataclass
class SemanticResult:
    materials: list[MaterialUnit] = field(default_factory=list)
    hits: list[dict] = field(default_factory=list)
    unindexed_hits: int = 0
    unresolved_hits: int = 0
    vector_index_key: str = ""
    index_version: str = ""


_channels: dict[str, grpc.Channel] = {}
_channels_lock = threading.Lock()


def _channel(target: str) -> grpc.Channel:
    """同机同目标共用一个 channel（gRPC channel 线程安全，自带连接池与重连）。"""
    with _channels_lock:
        channel = _channels.get(target)
        if channel is None:
            channel = grpc.insecure_channel(
                target,
                options=[
                    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
                ],
            )
            _channels[target] = channel
        return channel


def _translate(error: grpc.RpcError) -> SemanticSearchError:
    code = error.code()
    if code in UNAUTHENTICATED_CODES:
        # 令牌不符是部署配置问题：它不该被读成"索引不可用，稍后重试"。
        return SemanticSearchError(
            "semantic_index_unauthenticated", retryable=False, status=SURFACE_UNAVAILABLE_STATUS
        )
    if code in UNAVAILABLE_CODES:
        return SemanticSearchError(
            "semantic_index_unreachable", retryable=True, status=SURFACE_UNAVAILABLE_STATUS
        )
    # INTERNAL / UNKNOWN 之类：检索面答了，但答的不是本契约认识的东西。
    return SemanticSearchError(
        "semantic_index_protocol_error", retryable=False, status=SURFACE_REJECTED_STATUS
    )


def search(conn, settings, *, principal: str, query: str, limit: int) -> SemanticResult:
    """按查询文本检索；返回的 `hits` 与 `materials` 同序同长。"""
    target = settings.index_search_endpoint.strip()
    if not target:
        raise SemanticSearchError(
            "semantic_search_unavailable", retryable=False, status=SURFACE_UNAVAILABLE_STATUS
        )
    token = settings.index_search_token
    stub = index_pb2_grpc.IndexSearchServiceStub(_channel(target))
    request = index_pb2.SemanticSearchRequest(
        query=query,
        principal=principal,
        vector_index_key=settings.index_vector_index_key,
        limit=limit,
    )
    try:
        response = stub.SearchSemantic(
            request,
            timeout=settings.index_search_timeout_s,
            metadata=(("authorization", "Bearer " + token.get_secret_value()),),
        )
    except grpc.RpcError as error:
        raise _translate(error) from error
    if response.error.reason_code:
        # 原因码原样上抛：worker 的原因码词汇表就是这一层的词汇表。
        retryable = bool(response.error.retryable)
        raise SemanticSearchError(
            response.error.reason_code,
            retryable=retryable,
            status=SURFACE_UNAVAILABLE_STATUS if retryable else SURFACE_REJECTED_STATUS,
        )
    result = SemanticResult(
        unindexed_hits=int(response.unindexed_hits),
        vector_index_key=response.vector_index_key,
        index_version=response.index_version,
    )
    for hit in response.hits:
        material = materials.get_material(
            conn, principal, hit.material_unit_id, hit.material_revision
        )
        if material is None:
            result.unresolved_hits += 1
            continue
        result.materials.append(material)
        result.hits.append(
            {
                "material_unit_id": hit.material_unit_id,
                "revision": hit.material_revision,
                "distance": hit.distance,
                "embedding_id": hit.embedding_id,
                "vector_ref": hit.vector_ref,
                "observation_id": hit.observation_id,
            }
        )
    return result
