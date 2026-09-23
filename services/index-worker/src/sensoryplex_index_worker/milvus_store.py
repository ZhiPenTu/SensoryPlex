"""Milvus 端口：collection 契约、维度守卫、写入确认与近邻检索。

决策（ADR-020）：

- **collection 名就是 `vector_index_key`**（`material_text_<模型 slug>_d<实测维度>_v<契约版本>`）。
  换模型或换维度 = 新 collection；旧向量留在旧 collection，不原地迁移、不改写。
- **维度是身份的一部分**：写入前同时校验"payload 里的 dimension"、"collection 的 dim"与
  "向量实际长度"，三者不一致就显式失败，不截断、不补零、不假装是同一族向量。
- **索引类型 FLAT + COSINE**：向量已 L2 归一化，FLAT 是精确检索，验收可复算。
  HNSW/IVF 是性能决策，需要独立的召回测量，本切片不引入。
- **本模块不保存部署形态**：`vector_ref` 只写逻辑引用（collection + 主键），
  主机路径与端口属于部署配置，不进记录（ADR-010 §不外泄）。
- **Milvus Lite 是进程独占的**：同一数据目录被另一个进程 flock 持有时，本进程打不开
  （底层 `DataDirLockedError`）→ 预检成 `vector_store_locked`，不重试、不降级、不换路径。
  edge 形态因此是"单写进程"：写入者与检索者不能同时持有同一个目录。
"""

import os
import pathlib
import re

from pymilvus import DataType, MilvusClient

from .errors import IndexContractError, VectorStoreError


def local_store_locked(uri: str) -> bool:
    """Lite 形态：数据目录的 LOCK 是否已被（同机）别的进程持有？

    milvus-lite 的文档契约是"`__init__` 对 `{data_dir}/LOCK` 加 advisory flock"，
    被别人持有时 `MilvusClient` 只抛一个笼统的 `ConnectionConfigException
    ("Open local milvus failed")`——因为失败发生在起本地服务的线程里，异常链在那断掉。
    所以这里按同一个锁文件**预检**：既能给出"可重试"的稳定码 `vector_store_locked`，
    也避免白等一轮本地服务启动超时。

    探针只做判断，不改状态：拿不到锁就立刻判断为"被别人持有"。
    """
    try:
        import fcntl  # noqa: PLC0415 - Windows 没有 fcntl，缺了就按"不适用"处理

        from milvus_lite.db import LOCK_FILENAME  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 服务端形态 / 非 Unix
        return False
    if uri.startswith(("unix:", "http:", "https:", "tcp:")):
        return False  # 服务端形态没有本地数据目录
    data_dir = pathlib.Path(uri)
    lock_path = data_dir / LOCK_FILENAME
    if data_dir.suffix != ".db" or not lock_path.exists():
        return False  # 还没人开过这个目录（Lite 第一次打开时自己建 LOCK）
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    return False


INDEX_KEY_PATTERN = re.compile(
    r"^(?P<prefix>material_[a-z0-9]+(?:_[a-z0-9]+)*)"
    r"_d(?P<dimension>[1-9][0-9]*)"
    r"_v(?P<version>[1-9][0-9]*)$"
)
VECTOR_REF_SCHEME = "milvus"
VECTOR_REF_TEMPLATE = VECTOR_REF_SCHEME + "://{collection}/{embedding_id}"
MILVUS_REF_PATTERN = re.compile(r"^milvus://(?P<collection>[^/]+)/(?P<embedding_id>[^/]+)$")
EMBEDDING_ID_PATTERN = re.compile(r"^emb_[0-9a-f]{32}$")

# 字段顺序即写入契约。
SCALAR_FIELDS = (
    ("embedding_id", DataType.VARCHAR, 64, True),
    ("material_unit_id", DataType.VARCHAR, 64, False),
    ("material_revision", DataType.INT32, None, False),
    ("stream_id", DataType.VARCHAR, 64, False),
    ("start_ms", DataType.INT64, None, False),
    ("end_ms", DataType.INT64, None, False),
    ("modality", DataType.VARCHAR, 32, False),
    ("model_release_id", DataType.VARCHAR, 64, False),
    ("observation_id", DataType.VARCHAR, 64, False),
    ("content_hash", DataType.VARCHAR, 80, False),
    ("created_at_unix_ms", DataType.INT64, None, False),
)
VECTOR_FIELD = "vector"
INDEX_TYPE = "FLAT"
METRIC_TYPE = "COSINE"
OUTPUT_FIELDS = [name for name, *_ in SCALAR_FIELDS if name != "embedding_id"]


def parse_index_key(vector_index_key: str) -> tuple[str, int, int]:
    """返回 (prefix, dimension, contract_version)；不合规的 key 直接拒绝。"""
    match = INDEX_KEY_PATTERN.match(vector_index_key or "")
    if match is None:
        raise IndexContractError("invalid_vector_index_key", str(vector_index_key))
    return match["prefix"], int(match["dimension"]), int(match["version"])


def collection_name(vector_index_key: str) -> str:
    prefix, dimension, version = parse_index_key(vector_index_key)
    return f"{prefix}_d{dimension}_v{version}"


def vector_ref(collection: str, embedding_id: str) -> str:
    return VECTOR_REF_TEMPLATE.format(collection=collection, embedding_id=embedding_id)


def parse_vector_ref(ref: str) -> tuple[str, str]:
    match = MILVUS_REF_PATTERN.match(ref or "")
    if match is None:
        raise IndexContractError("invalid_vector_ref", str(ref))
    collection, embedding_id = match["collection"], match["embedding_id"]
    if not EMBEDDING_ID_PATTERN.match(embedding_id):
        raise IndexContractError("invalid_vector_ref", "embedding_id")
    try:
        parse_index_key(collection)
    except IndexContractError as error:
        # `milvus://host:19530/x/emb_…` 这类"带端点的引用"必须解析失败：
        # 否则"vector_ref 与部署形态无关"这条决策会在读取侧被绕过。
        raise IndexContractError("invalid_vector_ref", "collection") from error
    return collection, embedding_id


class VectorIndex:
    """一个 `vector_index_key` 对应一个 collection；实例只服务这一个 collection。"""

    def __init__(self, uri: str, vector_index_key: str):
        self.uri = uri
        self.vector_index_key = vector_index_key
        _, self.dimension, self.contract_version = parse_index_key(vector_index_key)
        self.collection = collection_name(vector_index_key)
        if local_store_locked(uri):
            # 别人正持有这个数据目录：这是"稍后重试"的容量约束，不是配置错误。
            # 只报文件名，不带主机路径（ADR-010 §不外泄）。
            raise VectorStoreError("vector_store_locked", pathlib.Path(uri).name)
        try:
            # Lite 形态传本地文件路径，服务端形态传 http(s):// 端点；客户端同一套 API。
            self._client = MilvusClient(uri=uri)
        except Exception as error:  # noqa: BLE001 - 底层异常文本不是契约，只暴露稳定码
            raise VectorStoreError("vector_store_unavailable", type(error).__name__) from error

    # ── collection 生命周期 ────────────────────────────────────────────────

    def ensure_collection(self) -> None:
        try:
            if self._client.has_collection(self.collection):
                self._verify_contract()
                return
            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
            for name, field_type, max_length, is_primary in SCALAR_FIELDS:
                if field_type is DataType.VARCHAR:
                    schema.add_field(name, field_type, max_length=max_length, is_primary=is_primary)
                else:
                    schema.add_field(name, field_type, is_primary=is_primary)
            schema.add_field(VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=self.dimension)
            params = self._client.prepare_index_params()
            params.add_index(
                field_name=VECTOR_FIELD, index_type=INDEX_TYPE, metric_type=METRIC_TYPE
            )
            self._client.create_collection(
                collection_name=self.collection, schema=schema, index_params=params
            )
        except (VectorStoreError, IndexContractError):
            # 契约不符是明确的业务判定，不能被下面的兜底包装成"创建失败"。
            raise
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError(
                "vector_collection_create_failed", type(error).__name__
            ) from error

    def _verify_contract(self) -> None:
        """已存在的 collection 必须与当前契约逐字段一致；不同就是不同，不做兼容猜测。"""
        try:
            described = self._client.describe_collection(self.collection)
            index = self._client.describe_index(self.collection, VECTOR_FIELD)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError(
                "vector_collection_describe_failed", type(error).__name__
            ) from error
        expected = {
            name: (field_type, max_length) for name, field_type, max_length, _ in SCALAR_FIELDS
        }
        expected[VECTOR_FIELD] = (DataType.FLOAT_VECTOR, self.dimension)
        actual = {}
        for field in described["fields"]:
            params = field.get("params") or {}
            width = (
                params.get("dim")
                if field["type"] == DataType.FLOAT_VECTOR
                else params.get("max_length")
            )
            actual[field["name"]] = (field["type"], width)
        if actual != expected:
            raise IndexContractError("vector_collection_contract_mismatch", self.collection)
        if (index["index_type"], index["metric_type"]) != (INDEX_TYPE, METRIC_TYPE):
            raise IndexContractError("vector_index_type_mismatch", self.collection)

    def _ensure_loaded(self) -> None:
        """读路径前必须先 load：新进程打开的 collection 处于 released 状态。

        缺少这一步时 query/search 会报 `collection is in state 'released'`——那是
        "没有加载"，不是"没有数据"，两者不能混为一谈。
        """
        try:
            self._client.load_collection(self.collection)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_collection_load_failed", type(error).__name__) from error

    def drop_collection(self) -> None:
        """只给验收脚本回滚用；生产路径不调用（删除必须有独立授权与保留策略）。"""
        self._client.drop_collection(self.collection)

    # ── 写入与确认 ─────────────────────────────────────────────────────────

    def upsert(self, records: list[dict]) -> int:
        if not records:
            return 0
        for record in records:
            vector = record.get(VECTOR_FIELD)
            if not isinstance(vector, list) or len(vector) != self.dimension:
                raise IndexContractError(
                    "vector_dimension_mismatch",
                    f"expected={self.dimension} actual={len(vector) if vector else 0}",
                )
        try:
            result = self._client.upsert(collection_name=self.collection, data=records)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_upsert_failed", type(error).__name__) from error
        return int(result.get("upsert_count", 0))

    def fetch(self, embedding_id: str) -> dict | None:
        """按主键取回记录；用于"写入确认"，不用于检索。"""
        try:
            self._ensure_loaded()
            rows = self._client.query(
                collection_name=self.collection,
                filter=f'embedding_id == "{embedding_id}"',
                output_fields=list(OUTPUT_FIELDS),
            )
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_query_failed", type(error).__name__) from error
        return dict(rows[0]) if rows else None

    # ── 检索 ──────────────────────────────────────────────────────────────

    def search(self, vector: list[float], limit: int) -> list[dict]:
        """只返回 (embedding_id, distance)；可见性与 ready 由调用方回查 PostgreSQL。"""
        if len(vector) != self.dimension:
            raise IndexContractError(
                "vector_dimension_mismatch", f"expected={self.dimension} actual={len(vector)}"
            )
        try:
            self._ensure_loaded()
            result = self._client.search(
                collection_name=self.collection,
                data=[list(vector)],
                limit=limit,
                output_fields=["content_hash"],
            )
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_search_failed", type(error).__name__) from error
        hits = []
        for row in result[0] if result else []:
            entity = row.get("entity") or {}
            hits.append(
                {
                    "embedding_id": row.get("embedding_id") or row.get("id"),
                    "distance": float(row["distance"]),
                    "content_hash": entity.get("content_hash"),
                }
            )
        return hits

    def count(self) -> int:
        try:
            self._ensure_loaded()
            rows = self._client.query(
                collection_name=self.collection, filter="", output_fields=["count(*)"]
            )
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_query_failed", type(error).__name__) from error
        return int(rows[0]["count(*)"]) if rows else 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已确认的写入结论
            pass
