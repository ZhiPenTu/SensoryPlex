"""embedding sink（index-worker）：把向量写进 Milvus，并把事实留在 PostgreSQL。

边界（ADR-020）：

- 本包**不读媒体、不碰数据面、不做推理**：向量由上游模型插件产出，这里只负责落库与检索。
- Milvus 只回答"哪条 embedding_id 离查询最近"；可见性与 `ready` 状态一律回查 PostgreSQL，
  Milvus 里的字段不作为鉴权依据。
- 写入失败必须显式失败：`state` 只可能是 `ready`（已确认写入）或 `failed`（带原因码）。
"""

from .errors import VectorStoreError
from .milvus_store import VectorIndex
from .worker import IndexOutcome, SearchOutcome, index_embedding, search_embeddings

__all__ = [
    "IndexOutcome",
    "SearchOutcome",
    "VectorIndex",
    "VectorStoreError",
    "index_embedding",
    "search_embeddings",
]
