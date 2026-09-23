"""embedding sink 的编排：上游 payload → 维度守卫 → Milvus → 确认 → PostgreSQL 事实。

顺序是有意的：**先写向量、确认写进去了，才把 `embedding_record.state` 置成 `ready`**。
任何一步失败都落到 `failed` 并带原因码；不存在"库没写成功但记录说可用"的中间态。
"""

import math
from dataclasses import dataclass, field

from . import records
from .errors import IndexContractError, VectorStoreError
from .milvus_store import VectorIndex, parse_index_key, vector_ref


@dataclass
class IndexOutcome:
    embedding_id: str
    vector_index_key: str
    collection: str
    dimension: int
    vector_ref: str
    state: str
    upserted: int
    confirmed: bool


@dataclass
class SearchOutcome:
    vector_index_key: str
    collection: str
    results: list[dict] = field(default_factory=list)
    unindexed_hits: int = 0


def extract_embedding(payload: dict) -> tuple[str, list[float], int, str, str]:
    """从模型插件 payload 里取出落库需要的五项；缺一项就拒绝，不做默认值猜测。"""
    if not isinstance(payload, dict):
        raise IndexContractError("invalid_embedding_payload", type(payload).__name__)
    vector = payload.get("vector")
    dimension = payload.get("dimension")
    index_key = payload.get("vector_index_key")
    observation_id = payload.get("embedding_id")
    content_hash = payload.get("text_sha256")
    if not isinstance(vector, list) or not vector:
        raise IndexContractError("invalid_embedding_payload", "vector")
    # 元素也必须真的是有限数：`["a"]` 或 `[nan]` 走到 numpy/faiss 才报错的话，
    # 失败会以 TypeError/底层异常的形式漏出去，不再是可分支的契约码。
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in vector
    ):
        raise IndexContractError("invalid_embedding_payload", "vector_element")
    # payload 是 protobuf Struct：整数在线上是 double，因此 512 会以 512.0 到达。
    # 这里接受"整数值"，但非整数、非正数一律拒绝——维度不许四舍五入。
    if not isinstance(dimension, (int, float)) or isinstance(dimension, bool):
        raise IndexContractError("invalid_embedding_payload", "dimension")
    if not float(dimension).is_integer() or float(dimension) <= 0:
        raise IndexContractError("invalid_embedding_payload", "dimension")
    dimension = int(dimension)
    if not isinstance(index_key, str) or not index_key:
        raise IndexContractError("invalid_embedding_payload", "vector_index_key")
    if not isinstance(observation_id, str) or not observation_id:
        raise IndexContractError("invalid_embedding_payload", "embedding_id")
    if not isinstance(content_hash, str) or not content_hash:
        raise IndexContractError("invalid_embedding_payload", "text_sha256")
    return observation_id, [float(value) for value in vector], dimension, index_key, content_hash


def index_embedding(
    conn,
    index: VectorIndex,
    *,
    payload: dict,
    material_unit_id: str,
    material_revision: int,
    model_release_id: str,
    stream_id: str,
    start_ms: int,
    end_ms: int,
    modality: str,
) -> IndexOutcome:
    observation_id, vector, dimension, index_key, content_hash = extract_embedding(payload)
    if index_key != index.vector_index_key:
        # 运行级配置错误：payload 认的 collection 与本次 worker 认的不是同一个。
        # 这里没有可信的身份（identity 里就含 vector_index_key），因此不留失败记录。
        raise IndexContractError("index_key_mismatch", f"{index_key}!={index.vector_index_key}")
    _, declared_dimension, _ = parse_index_key(index_key)
    embedding_id = records.embedding_id_for(observation_id, material_unit_id, material_revision)
    records.begin_pending(
        conn,
        embedding_id=embedding_id,
        material_unit_id=material_unit_id,
        material_revision=material_revision,
        model_release_id=model_release_id,
        observation_id=observation_id,
        vector_index_key=index_key,
        # 落库维度用 key 声明的那一个：它是 collection 契约的一部分。
        # payload 自称的维度只在下面参与校验，不写进事实行，免得被篡改值污染身份。
        dimension=declared_dimension,
        content_hash=content_hash,
    )
    try:
        # 三方一致：payload 声明的维度、payload 里的实测维度、向量实际长度。
        # 不一致必须留一条 failed 行，否则"拒绝了但库里查不到"就等于静默丢弃。
        if declared_dimension != dimension or len(vector) != dimension:
            raise IndexContractError(
                "vector_dimension_mismatch",
                f"key={declared_dimension} declared={dimension} actual={len(vector)}",
            )
        index.ensure_collection()
        upserted = index.upsert(
            [
                {
                    "embedding_id": embedding_id,
                    "material_unit_id": material_unit_id,
                    "material_revision": int(material_revision),
                    "stream_id": stream_id,
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "modality": modality,
                    "model_release_id": model_release_id,
                    "observation_id": observation_id,
                    "content_hash": content_hash,
                    "created_at_unix_ms": int(
                        conn.execute("SELECT now()").fetchone()[0].timestamp() * 1000
                    ),
                    "vector": vector,
                }
            ]
        )
        confirmed_row = index.fetch(embedding_id)
        if (
            confirmed_row is None
            or confirmed_row.get("content_hash") != content_hash
            or confirmed_row.get("material_unit_id") != material_unit_id
        ):
            raise VectorStoreError("vector_confirm_mismatch", embedding_id)
    except (VectorStoreError, IndexContractError) as error:
        records.mark_failed(conn, embedding_id, error_code=error.code)
        raise
    reference = vector_ref(index.collection, embedding_id)
    records.mark_ready(conn, embedding_id, vector_ref=reference)
    return IndexOutcome(
        embedding_id=embedding_id,
        vector_index_key=index_key,
        collection=index.collection,
        dimension=dimension,
        vector_ref=reference,
        state="ready",
        upserted=upserted,
        confirmed=True,
    )


def search_embeddings(
    conn, index: VectorIndex, *, vector: list[float], limit: int, principal: str
) -> SearchOutcome:
    """Milvus 命中 → PostgreSQL 回查。查不到 ready 记录的命中被丢弃并计数。"""
    hits = index.search(vector, limit)
    resolved = records.resolve_ready(
        conn, principal, [hit["embedding_id"] for hit in hits], limit=limit
    )
    results = []
    for hit in hits:
        record = resolved.get(hit["embedding_id"])
        if record is None:
            continue
        results.append({**record, "distance": hit["distance"]})
    return SearchOutcome(
        vector_index_key=index.vector_index_key,
        collection=index.collection,
        results=results,
        unindexed_hits=len(hits) - len(results),
    )
