"""索引检索面（ADR-023）：常驻进程里**唯一**打开向量库的地方。

调用者只提交查询文本。这一侧固定做四件事，顺序就是决策：

1. 鉴权：共享令牌 + 常量时间比较（检索面一旦跨容器接入就必须显式开端口，不能裸奔）；
2. 请求准入：查询文本、principal、collection、条数上限全部显式校验，不做默认值猜测；
3. 同源守卫：先确认 collection 里的向量与本次查询编码器出自**同一个模型身份**，再编码；
4. 近邻 + 事实回查：Milvus 只回答"哪条 embedding_id 最近"，能不能返回由 PostgreSQL 决定
   （`state='ready'` + material 存在且未失败 + `source.owner` 等于 principal），被丢弃的命中
   单独计数（ADR-020 §2）。

刻意不做的事：不水合素材本体（调用方按 `(material_unit_id, revision)` 自己回查，事实水合
只有一处实现），不做 RRF/混合检索，不做相关性校准——距离是 COSINE 距离，不是置信度。
"""

from __future__ import annotations

import hmac
from concurrent import futures

import grpc
import psycopg
from edge_material_sdk import get_logger
from edge_material_sdk.generated.index.v1 import index_pb2, index_pb2_grpc
from psycopg_pool import ConnectionPool

from . import records
from .errors import IndexContractError, VectorStoreError
from .milvus_store import VectorIndex
from .query_encoder import QueryEncoder, QueryEncoderError
from .worker import search_embeddings

LOGGER = get_logger("sensoryplex.index.serve")

# 检索语义版本：写清楚这批命中怎么来的。改了编码/度量/回查语义就要改这个串。
INDEX_VERSION = "milvus-flat-cosine-v1"
MAX_QUERY_LENGTH = 2000
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MIN_AUTH_TOKEN_LENGTH = 32
AUTHORIZATION_METADATA = "authorization"
BEARER_PREFIX = "Bearer "
# 瞬时容量约束 vs 配置/契约错误：前者"稍后重试可能成功"，后者要人去改东西。
RETRYABLE_CODES = frozenset(
    {
        "vector_store_locked",
        "vector_store_unavailable",
        "vector_collection_load_failed",
        "vector_search_failed",
        "vector_query_failed",
    }
)
STATEMENT_TIMEOUT_MS = 5_000


def _session_options(database_url: str) -> str:
    """在 DSN 已有的 `options` 之后**追加** statement_timeout，而不是覆盖它。

    覆盖会静默丢掉调用方给的会话设置（例如隔离 schema 用的 `-c search_path=...`），
    表现为"连上了、却查不到表"——连接池把配置吃掉了，调用方只能看到一句 `UndefinedTable`。
    """
    existing = psycopg.conninfo.conninfo_to_dict(database_url).get("options", "")
    timeout = f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
    return f"{existing} {timeout}".strip() if existing else timeout


def _response(**overrides) -> index_pb2.SemanticSearchResponse:
    fields = {
        "hits": [],
        "vector_index_key": "",
        "collection": "",
        "index_version": INDEX_VERSION,
        "unindexed_hits": 0,
        "query_model_release_id": "",
        "query_dimension": 0,
    }
    fields.update(overrides)
    return index_pb2.SemanticSearchResponse(**fields)


def failure(code: str, detail: str = "") -> index_pb2.SemanticSearchResponse:
    """整请求失败：一个稳定的顶层原因码，不伪造空命中列表当"没有结果"。"""
    return _response(
        error=index_pb2.IndexFailure(
            reason_code=code, detail=detail[:200], retryable=code in RETRYABLE_CODES
        )
    )


class IndexSearchServicer(index_pb2_grpc.IndexSearchServiceServicer):
    def __init__(
        self,
        *,
        pool: ConnectionPool,
        index: VectorIndex,
        encoder: QueryEncoder,
        auth_token: str,
    ):
        self._pool = pool
        self._index = index
        self._encoder = encoder
        self._auth_token = auth_token

    # ── 鉴权与准入 ─────────────────────────────────────────────────────────

    def _authorize(self, context) -> None:
        """共享令牌；缺失或不符一律 `PERMISSION_DENIED`，不区分"没带"与"带错"。"""
        presented = ""
        for item in context.invocation_metadata():
            if item.key == AUTHORIZATION_METADATA and item.value.startswith(BEARER_PREFIX):
                presented = item.value[len(BEARER_PREFIX) :]
                break
        if not presented or not hmac.compare_digest(presented, self._auth_token):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "index_auth_failed")

    def _admit(self, request) -> tuple[str, str] | None:
        if not request.query or len(request.query) > MAX_QUERY_LENGTH:
            return "invalid_query", f"length={len(request.query)}"
        if not request.principal or len(request.principal) > 128:
            return "invalid_principal", f"length={len(request.principal)}"
        if request.vector_index_key != self._index.vector_index_key:
            return "vector_index_key_mismatch", request.vector_index_key
        if request.limit > MAX_LIMIT:
            return "invalid_limit", str(request.limit)
        return None

    def _same_origin(self, conn) -> tuple[str, str] | None:
        """同源守卫：collection 里的模型身份必须**唯一且等于**本次查询编码器（ADR-023）。"""
        releases = records.collection_model_releases(conn, self._index.vector_index_key, limit=2)
        if len(releases) > 1:
            return "vector_index_model_release_mixed", ",".join(releases)[:120]
        if releases and releases[0] != self._encoder.release_id:
            # 只给出两个身份，不带权重目录：身份本身就是权重摘要的前缀。
            return (
                "query_model_release_mismatch",
                f"index={releases[0]} encoder={self._encoder.release_id}"[:120],
            )
        return None

    # ── 检索 ───────────────────────────────────────────────────────────────

    def SearchSemantic(self, request, context):
        self._authorize(context)
        rejected = self._admit(request)
        if rejected is not None:
            return failure(*rejected)
        limit = request.limit or DEFAULT_LIMIT
        with self._pool.connection() as conn:
            guarded = self._same_origin(conn)
            if guarded is not None:
                return failure(*guarded)
            try:
                vector = self._encoder.encode(request.query)
            except QueryEncoderError as error:
                return failure(error.code, error.detail)
            try:
                outcome = search_embeddings(
                    conn,
                    self._index,
                    vector=vector,
                    limit=limit,
                    principal=request.principal,
                )
            except (VectorStoreError, IndexContractError) as error:
                return failure(error.code, error.detail)
        return _response(
            hits=[
                index_pb2.SemanticHit(
                    embedding_id=row["embedding_id"],
                    material_unit_id=row["material_unit_id"],
                    material_revision=int(row["material_revision"]),
                    distance=float(row["distance"]),
                    vector_ref=row["vector_ref"] or "",
                    observation_id=row["observation_id"] or "",
                    model_release_id=row["model_release_id"],
                    dimension=int(row["dimension"]),
                )
                for row in outcome.results
            ],
            vector_index_key=outcome.vector_index_key,
            collection=outcome.collection,
            unindexed_hits=outcome.unindexed_hits,
            query_model_release_id=self._encoder.release_id,
            query_dimension=self._encoder.dimension,
        )


def start_server(
    *,
    database_url: str,
    index: VectorIndex,
    encoder: QueryEncoder,
    auth_token: str,
    bind: str,
    port: int,
    max_concurrency: int,
) -> tuple[grpc.Server, ConnectionPool]:
    """装配并启动检索面：并发与连接池都有上限，鉴权令牌必须是显式提供的强令牌。"""
    if len(auth_token) < MIN_AUTH_TOKEN_LENGTH:
        raise VectorStoreError("index_auth_token_too_short", f"min={MIN_AUTH_TOKEN_LENGTH}")
    if encoder.vector_index_key != index.vector_index_key:
        # 编码器按自己的权重推出来的 collection 与本次服务的不是同一个：这是配置错误，
        # 一旦放过就等于拿 A 模型的查询向量去搜 B 模型的 collection。
        raise IndexContractError(
            "query_encoder_index_key_mismatch",
            f"{encoder.vector_index_key}!={index.vector_index_key}",
        )
    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_concurrency,
        timeout=3,
        max_waiting=max_concurrency,
        kwargs={"connect_timeout": 3, "options": _session_options(database_url)},
    )
    pool.open()
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as error:
        pool.close()
        raise VectorStoreError("metadata_store_unavailable", type(error).__name__) from error
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_concurrency),
        options=[
            ("grpc.max_receive_message_length", 1 << 20),
            ("grpc.max_send_message_length", 4 << 20),
        ],
    )
    index_pb2_grpc.add_IndexSearchServiceServicer_to_server(
        IndexSearchServicer(pool=pool, index=index, encoder=encoder, auth_token=auth_token),
        server,
    )
    if server.add_insecure_port(f"{bind}:{port}") == 0:
        pool.close()
        raise VectorStoreError("index_bind_failed", f"{bind}:{port}")
    server.start()
    LOGGER.info(
        "Started index search gRPC service", bind=bind, port=port, max_concurrency=max_concurrency
    )
    return server, pool
