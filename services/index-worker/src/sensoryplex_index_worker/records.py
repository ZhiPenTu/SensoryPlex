"""embedding_record 的读写与检索二次过滤（PostgreSQL 侧事实）。

Milvus 不是事实源：它只回答"哪条 embedding_id 离查询最近"。一条命中要被返回，必须同时
满足 PostgreSQL 里的 `state='ready'`、material 行存在、并且当前 principal 是该 source 的
owner——否则丢弃并计数（`unindexed_hits`），不得把 Milvus 里的字段当授权依据。
"""

import hashlib

LATEST = """NOT EXISTS (SELECT 1 FROM material_unit newer
    WHERE newer.material_unit_id=m.material_unit_id AND newer.revision>m.revision)"""

READY_QUERY = f"""SELECT e.embedding_id, e.material_unit_id, e.material_revision,
       e.model_release_id, e.dimension, e.content_hash, e.vector_ref, e.vector_index_key,
       e.observation_id, e.indexed_at,
       m.stream_id, m.start_ms, m.end_ms, m.status,
       NOT ({LATEST}) AS superseded
    FROM embedding_record e
    JOIN material_unit m
      ON m.material_unit_id=e.material_unit_id AND m.revision=e.material_revision
    JOIN stream_session s ON s.stream_id=m.stream_id
    JOIN media_source source ON source.source_id=s.source_id
    WHERE e.state='ready' AND source.owner=%s AND m.status <> 'failed'"""


def embedding_id_for(observation_id: str, material_unit_id: str, revision: int) -> str:
    """确定性主键：同一条观测在同一 material revision 下重跑得到同一个 id（可幂等重跑）。

    观测可以被多个 material 引用（`material_observation` 是多对多），因此主键里必须带
    material 作用域，否则第二次引用会撞 PK。代价是同一段文字在多个 material 下会各存一份
    向量；共享向量的表结构是后续优化项，不在本切片。
    """
    seed = f"{observation_id}|{material_unit_id}|{revision}"
    return "emb_" + hashlib.sha256(seed.encode()).hexdigest()[:32]


IDENTITY = ("material_unit_id", "material_revision", "observation_id", "vector_index_key")


class IdentityConflict(ValueError):
    pass


def begin_pending(
    conn,
    *,
    embedding_id: str,
    material_unit_id: str,
    material_revision: int,
    model_release_id: str,
    observation_id: str,
    vector_index_key: str,
    dimension: int,
    content_hash: str,
) -> None:
    """先落一条 pending；已存在则校验身份一致（不一致就是冲突，不覆盖别人的记录）。"""
    conn.execute(
        "INSERT INTO embedding_record(embedding_id,material_unit_id,material_revision,"
        "model_release_id,observation_id,vector_index_key,dimension,content_hash,state) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT (embedding_id) DO NOTHING",
        (
            embedding_id,
            material_unit_id,
            material_revision,
            model_release_id,
            observation_id,
            vector_index_key,
            dimension,
            content_hash,
        ),
    )
    row = conn.execute(
        "SELECT material_unit_id, material_revision, observation_id, vector_index_key, "
        "model_release_id, dimension, content_hash FROM embedding_record WHERE embedding_id=%s",
        (embedding_id,),
    ).fetchone()
    if row[:4] != (material_unit_id, material_revision, observation_id, vector_index_key):
        raise IdentityConflict("embedding_identity_conflict")
    if row[4:] != (model_release_id, dimension, content_hash):
        raise IdentityConflict("embedding_payload_conflict")


def mark_ready(conn, embedding_id: str, *, vector_ref: str) -> None:
    """确认写入之后才允许置 ready；`indexed_at` 与 `vector_ref` 一起写。"""
    updated = conn.execute(
        "UPDATE embedding_record SET state='ready', vector_ref=%s, error_code=NULL, "
        "indexed_at=now(), updated_at=now() WHERE embedding_id=%s RETURNING embedding_id",
        (vector_ref, embedding_id),
    ).fetchone()
    if not updated:
        raise IdentityConflict("embedding_record_missing")


def mark_failed(conn, embedding_id: str, *, error_code: str) -> None:
    """失败必须留原因码；把已经 ready 的行改成 failed 只可能发生在重跑失败时。

    刻意**不动** `vector_ref` / `indexed_at`：它们描述"最近一次确认写入"，`state` 描述
    "最近一次尝试"。重跑失败不该抹掉"库里那份向量仍然存在"这个事实——而检索只认
    `state='ready'`，所以保留引用不会让它被当成功命中返回（验收场景 5 就在钉这一条）。
    """
    conn.execute(
        "UPDATE embedding_record SET state='failed', error_code=%s, updated_at=now() "
        "WHERE embedding_id=%s",
        (error_code, embedding_id),
    )


def record_state(conn, embedding_id: str) -> dict | None:
    row = conn.execute(
        "SELECT state, vector_ref, error_code, indexed_at, dimension, content_hash "
        "FROM embedding_record WHERE embedding_id=%s",
        (embedding_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "state": row[0],
        "vector_ref": row[1],
        "error_code": row[2],
        "indexed_at": row[3].isoformat() if row[3] else None,
        "dimension": row[4],
        "content_hash": row[5],
    }


def resolve_ready(conn, principal: str, embedding_ids: list[str], *, limit: int) -> dict[str, dict]:
    """把 Milvus 命中回查成可返回的记录；查不到的命中由调用方计入 unindexed_hits。"""
    if not embedding_ids:
        return {}
    rows = conn.execute(
        f"{READY_QUERY} AND e.embedding_id = ANY(%s) LIMIT %s",
        (principal, embedding_ids, limit),
    ).fetchall()
    return {
        row[0]: {
            "embedding_id": row[0],
            "material_unit_id": row[1],
            "material_revision": row[2],
            "model_release_id": row[3],
            "dimension": row[4],
            "content_hash": row[5],
            "vector_ref": row[6],
            "vector_index_key": row[7],
            "observation_id": row[8],
            "indexed_at": row[9].isoformat() if row[9] else None,
            "stream_id": row[10],
            "start_ms": row[11],
            "end_ms": row[12],
            "status": row[13],
            "superseded": row[14],
        }
        for row in rows
    }


def ready_ids(conn, principal: str, vector_index_key: str) -> list[str]:
    rows = conn.execute(
        "SELECT e.embedding_id FROM embedding_record e "
        "JOIN material_unit m ON m.material_unit_id=e.material_unit_id "
        "AND m.revision=e.material_revision "
        "JOIN stream_session s ON s.stream_id=m.stream_id "
        "JOIN media_source source ON source.source_id=s.source_id "
        "WHERE e.state='ready' AND e.vector_index_key=%s AND source.owner=%s "
        "AND m.status <> 'failed'",
        (vector_index_key, principal),
    ).fetchall()
    return [row[0] for row in rows]


def collection_model_releases(conn, vector_index_key: str, *, limit: int = 2) -> list[str]:
    """这个 collection 里已确认写入的模型身份（去重、按 key 名的顺序，最多 limit 个）。

    检索面用它做**同源守卫**：距离只有在查询向量与索引向量出自同一份模型时才有意义。
    刻意不按 principal 过滤——collection 是所有 owner 共用的，混进另一个 release 的向量会
    让所有人的距离失去可比性，因此这里的一致性是 collection 级，不是租户级的。

    只回 release id（形如 `bge:<模型>@<权重摘要前缀>`），不带 owner、不带路径。
    """
    rows = conn.execute(
        "SELECT model_release_id FROM embedding_record "
        "WHERE vector_index_key=%s AND state='ready' "
        "GROUP BY model_release_id ORDER BY model_release_id LIMIT %s",
        (vector_index_key, limit),
    ).fetchall()
    return [row[0] for row in rows]
