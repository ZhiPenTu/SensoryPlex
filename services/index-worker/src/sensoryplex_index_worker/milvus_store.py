"""向量存储端口与存储引擎适配层 (Storage Engine Adapter)。

支持两种存储引擎形态：
1. **MilvusStoreAdapter**：
   - Milvus Lite（本地文件模式，如 `milvus.db`）：单进程持有 flock 排他锁，适用于轻量单机/边缘端；
   - Milvus Standalone / Cluster（服务端网络模式，如 `http://...` 或 `tcp://...`）：
     无本地进程排他锁，支持多节点横向扩展、读写分离与高并发检索。
2. **PgVectorStoreAdapter**：
   - 基于 PostgreSQL 关系型与向量能力（pgvector 扩展或不可变余弦距离函数）；
   - 与业务事实数据库共享同一事务环境与连接池，天然实现事务原子一致性，消除跨进程数据不同步。

决策（ADR-020）：
- collection 名就是 `vector_index_key`（`material_text_<模型 slug>_d<实测维度>_v<契约版本>`）。
- 维度是身份的一部分：写入前校验契约维度、实测维度与向量长度。
- 索引类型 FLAT + COSINE（向量已 L2 归一化）。
- vector_ref 格式为 `<scheme>://{collection}/{embedding_id}`，不暴露物理主机或端口。
"""

from __future__ import annotations

import os
import pathlib
import re
from abc import ABC, abstractmethod
from typing import Any

import psycopg
from pymilvus import DataType, MilvusClient

from .errors import IndexContractError, VectorStoreError


def local_store_locked(uri: str) -> bool:
    """Lite 形态：数据目录的 LOCK 是否已被（同机）别的进程持有？"""
    try:
        import fcntl  # noqa: PLC0415

        from milvus_lite.db import LOCK_FILENAME  # noqa: PLC0415
    except ImportError:
        return False
    if uri.startswith(
        ("unix:", "http:", "https:", "tcp:", "postgresql:", "postgres:", "pgvector:")
    ):
        return False
    data_dir = pathlib.Path(uri)
    lock_path = data_dir / LOCK_FILENAME
    if data_dir.suffix != ".db" or not lock_path.exists():
        return False
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
VECTOR_REF_TEMPLATE = "{scheme}://{collection}/{embedding_id}"
MILVUS_REF_PATTERN = re.compile(
    r"^(?P<scheme>milvus|pgvector)://(?P<collection>[^/]+)/(?P<embedding_id>[^/]+)$"
)
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


def vector_ref(collection: str, embedding_id: str, scheme: str = VECTOR_REF_SCHEME) -> str:
    return VECTOR_REF_TEMPLATE.format(
        scheme=scheme, collection=collection, embedding_id=embedding_id
    )


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
        raise IndexContractError("invalid_vector_ref", "collection") from error
    return collection, embedding_id


class BaseVectorStoreAdapter(ABC):
    """向量存储引擎抽象适配层 (Storage Engine Adapter)。"""

    @abstractmethod
    def ensure_collection(self) -> None:
        """确保集合/表就绪并符合维度契约。"""

    @abstractmethod
    def drop_collection(self) -> None:
        """删除集合/表（仅验收或测试使用）。"""

    @abstractmethod
    def upsert(self, records: list[dict]) -> int:
        """批量写入或更新向量。"""

    @abstractmethod
    def fetch(self, embedding_id: str) -> dict | None:
        """按主键获取已写入记录。"""

    @abstractmethod
    def search(self, vector: list[float], limit: int) -> list[dict]:
        """按余弦相似度检索最近邻向量。"""

    @abstractmethod
    def count(self) -> int:
        """当前集合内向量总数。"""

    @abstractmethod
    def delete(self, embedding_ids: list[str]) -> int:
        """批量物理删除向量。"""

    @abstractmethod
    def close(self) -> None:
        """关闭存储连接。"""

    @property
    @abstractmethod
    def is_shared(self) -> bool:
        """是否支持跨进程/水平扩展无排他锁访问。"""

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """存储引擎标识。"""


class MilvusStoreAdapter(BaseVectorStoreAdapter):
    """Milvus 适配器：支持 Lite（文件锁）与 Standalone/Cluster（网络端点）。"""

    def __init__(self, uri: str, vector_index_key: str, token: str = ""):
        self.uri = uri
        self.vector_index_key = vector_index_key
        _, self.dimension, self.contract_version = parse_index_key(vector_index_key)
        self.collection = collection_name(vector_index_key)

        is_remote = uri.startswith(("unix:", "http:", "https:", "tcp:"))
        if not is_remote:
            data_dir = pathlib.Path(uri)
            data_dir.parent.mkdir(parents=True, exist_ok=True)
            if local_store_locked(uri):
                raise VectorStoreError("vector_store_locked", data_dir.name)

        try:
            kwargs: dict[str, Any] = {"uri": uri}
            if token:
                kwargs["token"] = token
            self._client = MilvusClient(**kwargs)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_store_unavailable", type(error).__name__) from error

        self._is_shared = is_remote
        self._engine_name = "milvus_server" if is_remote else "milvus_lite"

    @property
    def is_shared(self) -> bool:
        return self._is_shared

    @property
    def engine_name(self) -> str:
        return self._engine_name

    def _verify_contract(self) -> None:
        try:
            described = self._client.describe_collection(self.collection)
            index = self._client.describe_index(self.collection, index_name=VECTOR_FIELD)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError(
                "vector_collection_contract_mismatch", type(error).__name__
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
        try:
            self._client.load_collection(self.collection)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_collection_load_failed", type(error).__name__) from error

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
        except IndexContractError:
            raise
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError(
                "vector_collection_create_failed", type(error).__name__
            ) from error

    def drop_collection(self) -> None:
        self._client.drop_collection(self.collection)

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

    def search(self, vector: list[float], limit: int) -> list[dict]:
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

    def delete(self, embedding_ids: list[str]) -> int:
        if not embedding_ids:
            return 0
        self._ensure_loaded()
        try:
            result = self._client.delete(collection_name=self.collection, ids=embedding_ids)
            if isinstance(result, dict) and "delete_count" in result:
                return int(result["delete_count"])
            return len(embedding_ids)
        except Exception:
            try:
                quoted = ", ".join(f'"{i}"' for i in embedding_ids)
                result = self._client.delete(
                    collection_name=self.collection, filter=f"embedding_id in [{quoted}]"
                )
                if isinstance(result, dict) and "delete_count" in result:
                    return int(result["delete_count"])
                return len(embedding_ids)
            except Exception as error:
                raise VectorStoreError("vector_delete_failed", type(error).__name__) from error

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


class PgVectorStoreAdapter(BaseVectorStoreAdapter):
    """PostgreSQL 存储引擎适配器：支持中小规模集群事务级原子一致与水平读扩展。"""

    def __init__(self, uri: str, vector_index_key: str):
        self.uri = uri
        self.vector_index_key = vector_index_key
        _, self.dimension, self.contract_version = parse_index_key(vector_index_key)
        self.collection = collection_name(vector_index_key)
        self.table_name = f"vec_{self.collection.replace('-', '_')}"

        # 归一化 DSN 协议头
        connect_uri = uri
        if connect_uri.startswith("pgvector://"):
            connect_uri = "postgresql://" + connect_uri[len("pgvector://") :]

        try:
            self._conn = psycopg.connect(connect_uri, autocommit=True)
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_store_unavailable", type(error).__name__) from error

    @property
    def is_shared(self) -> bool:
        return True

    @property
    def engine_name(self) -> str:
        return "pgvector"

    def ensure_collection(self) -> None:
        try:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    embedding_id text PRIMARY KEY,
                    material_unit_id text NOT NULL,
                    material_revision integer NOT NULL,
                    stream_id text NOT NULL,
                    start_ms bigint NOT NULL,
                    end_ms bigint NOT NULL,
                    modality text NOT NULL,
                    model_release_id text NOT NULL,
                    observation_id text NOT NULL,
                    content_hash text NOT NULL,
                    created_at_unix_ms bigint NOT NULL,
                    vector float8[] NOT NULL
                );
                CREATE INDEX IF NOT EXISTS {self.table_name}_mat_idx
                    ON {self.table_name}(material_unit_id, material_revision);
                """
            )
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError(
                "vector_collection_create_failed", type(error).__name__
            ) from error

    def drop_collection(self) -> None:
        self._conn.execute(f"DROP TABLE IF EXISTS {self.table_name}")

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
        count = 0
        for record in records:
            self._conn.execute(
                f"""
                INSERT INTO {self.table_name} (
                    embedding_id, material_unit_id, material_revision, stream_id,
                    start_ms, end_ms, modality, model_release_id, observation_id,
                    content_hash, created_at_unix_ms, vector
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (embedding_id) DO UPDATE SET
                    material_unit_id = EXCLUDED.material_unit_id,
                    material_revision = EXCLUDED.material_revision,
                    stream_id = EXCLUDED.stream_id,
                    start_ms = EXCLUDED.start_ms,
                    end_ms = EXCLUDED.end_ms,
                    modality = EXCLUDED.modality,
                    model_release_id = EXCLUDED.model_release_id,
                    observation_id = EXCLUDED.observation_id,
                    content_hash = EXCLUDED.content_hash,
                    created_at_unix_ms = EXCLUDED.created_at_unix_ms,
                    vector = EXCLUDED.vector
                """,
                (
                    record["embedding_id"],
                    record["material_unit_id"],
                    int(record["material_revision"]),
                    record["stream_id"],
                    int(record["start_ms"]),
                    int(record["end_ms"]),
                    record["modality"],
                    record["model_release_id"],
                    record["observation_id"],
                    record["content_hash"],
                    int(record["created_at_unix_ms"]),
                    record["vector"],
                ),
            )
            count += 1
        return count

    def fetch(self, embedding_id: str) -> dict | None:
        row = self._conn.execute(
            f"""
            SELECT embedding_id, material_unit_id, material_revision, stream_id,
                   start_ms, end_ms, modality, model_release_id, observation_id,
                   content_hash, created_at_unix_ms
            FROM {self.table_name} WHERE embedding_id = %s
            """,
            (embedding_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "embedding_id": row[0],
            "material_unit_id": row[1],
            "material_revision": row[2],
            "stream_id": row[3],
            "start_ms": row[4],
            "end_ms": row[5],
            "modality": row[6],
            "model_release_id": row[7],
            "observation_id": row[8],
            "content_hash": row[9],
            "created_at_unix_ms": row[10],
        }

    def search(self, vector: list[float], limit: int) -> list[dict]:
        if len(vector) != self.dimension:
            raise IndexContractError(
                "vector_dimension_mismatch", f"expected={self.dimension} actual={len(vector)}"
            )
        try:
            rows = self._conn.execute(
                f"""
                SELECT embedding_id, content_hash, vector_cosine_distance(vector, %s) AS distance
                FROM {self.table_name}
                ORDER BY distance ASC
                LIMIT %s
                """,
                (vector, limit),
            ).fetchall()
        except Exception as error:  # noqa: BLE001
            raise VectorStoreError("vector_search_failed", type(error).__name__) from error
        return [
            {
                "embedding_id": row[0],
                "content_hash": row[1],
                "distance": float(row[2]),
            }
            for row in rows
        ]

    def count(self) -> int:
        row = self._conn.execute(f"SELECT count(*) FROM {self.table_name}").fetchone()
        return int(row[0]) if row else 0

    def delete(self, embedding_ids: list[str]) -> int:
        if not embedding_ids:
            return 0
        rows = self._conn.execute(
            f"DELETE FROM {self.table_name} WHERE embedding_id = ANY(%s) RETURNING embedding_id",
            (embedding_ids,),
        ).fetchall()
        return len(rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


class VectorIndex:
    """统一向量索引门面 (Facade)，内部根据 URI 与配置委托给具体适配器。"""

    def __init__(
        self,
        uri: str,
        vector_index_key: str,
        *,
        engine: str | None = None,
        token: str = "",
    ):
        self.uri = uri
        self.vector_index_key = vector_index_key
        _, self.dimension, self.contract_version = parse_index_key(vector_index_key)
        self.collection = collection_name(vector_index_key)

        is_pg = engine == "pgvector" or uri.startswith(("postgresql:", "postgres:", "pgvector:"))
        if is_pg:
            self._adapter: BaseVectorStoreAdapter = PgVectorStoreAdapter(
                uri=uri, vector_index_key=vector_index_key
            )
        else:
            self._adapter = MilvusStoreAdapter(
                uri=uri, vector_index_key=vector_index_key, token=token
            )

    @property
    def is_shared(self) -> bool:
        return self._adapter.is_shared

    @property
    def engine_name(self) -> str:
        return self._adapter.engine_name

    @property
    def adapter(self) -> BaseVectorStoreAdapter:
        return self._adapter

    @property
    def _client(self) -> Any:
        return getattr(self._adapter, "_client", None)

    def ensure_collection(self) -> None:
        self._adapter.ensure_collection()

    def drop_collection(self) -> None:
        self._adapter.drop_collection()

    def upsert(self, records: list[dict]) -> int:
        return self._adapter.upsert(records)

    def fetch(self, embedding_id: str) -> dict | None:
        return self._adapter.fetch(embedding_id)

    def search(self, vector: list[float], limit: int) -> list[dict]:
        return self._adapter.search(vector, limit)

    def count(self) -> int:
        return self._adapter.count()

    def delete(self, embedding_ids: list[str]) -> int:
        return self._adapter.delete(embedding_ids)

    def close(self) -> None:
        self._adapter.close()
