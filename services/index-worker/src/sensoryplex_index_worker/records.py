"""embedding_record 的读写与检索二次过滤（PostgreSQL 侧事实）。

Milvus 不是事实源：它只回答"哪条 embedding_id 离查询最近"。一条命中要被返回，必须同时
满足 PostgreSQL 里的 `state='ready'`、material 行存在、并且当前 principal 是该 source 的
owner——否则丢弃并计数（`unindexed_hits`），不得把 Milvus 里的字段当授权依据。

本模块同时承载 outbox 事件的**消费去重**原语（`consumed_event`，见 ADR-024）：它和
`embedding_record` 同属"PostgreSQL 侧事实"，且是同一个 sink 在同一个事务里写的。
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


# ── outbox 事件的消费去重（ADR-024） ─────────────────────────────────────────
#
# `consumed_event(event_id, consumer_name, consumed_at)` 的主键就是幂等键。
# 表里 `consumed_at` 是 `NOT NULL DEFAULT now()`，**没有**"在飞（in-flight）"这一态：只要
# 插进去就等于"这个 consumer 已经处理完了"。因此这里刻意**不**提供"先认领、再干活、最后
# 收尾"的两相接口——那样会在"插了行、还没干完"的窗口里崩溃，而重投时那行会让事件被**永久
# 跳过**（悄悄把 at-least-once 变成 at-most-once，事件丢了也没人知道）。正确顺序是反的：
#
#     1. 先做幂等工作（`embedding_id` 是确定性的，`begin_pending` 撞 PK 会校验身份，
#        重跑得到同一份向量）；
#     2. 干完了再 `record_consumed`。
#
# 于是崩溃最多让工作重做一遍，绝不会让事件消失；`consumed_at` 也回到它字面上的意思——
# "这个 consumer 在哪一刻处理完的"。


def is_consumed(conn, *, event_id: str, consumer_name: str) -> bool:
    """本 consumer 是否已经把这条事件处理完（用于跳过重复投递带来的重复工作）。"""
    return (
        conn.execute(
            "SELECT 1 FROM consumed_event WHERE event_id=%s AND consumer_name=%s",
            (event_id, consumer_name),
        ).fetchone()
        is not None
    )


def record_consumed(conn, *, event_id: str, consumer_name: str) -> bool:
    """记录"处理完成"。True = 本次写入（首次完成）；False = 之前已记录（幂等的重复完成）。

    重复完成**不是**错误：JetStream 的重投与 relay 的重复发布（ADR-024 §4）都会让同一条事件
    再来一次，此时 `False` 是预期结果，调用方应把它当"无需再做什么"，而不是失败。
    """
    row = conn.execute(
        "INSERT INTO consumed_event(event_id, consumer_name) VALUES (%s, %s) "
        "ON CONFLICT (event_id, consumer_name) DO NOTHING RETURNING event_id",
        (event_id, consumer_name),
    ).fetchone()
    return row is not None


def consumed_state(conn, *, event_id: str, consumer_name: str) -> dict | None:
    """这条事件在本 consumer 视角下的记账状态；`None` 表示从未处理过（可重投）。"""
    row = conn.execute(
        "SELECT consumed_at FROM consumed_event WHERE event_id=%s AND consumer_name=%s",
        (event_id, consumer_name),
    ).fetchone()
    if row is None:
        return None
    return {
        "event_id": event_id,
        "consumer_name": consumer_name,
        "consumed_at": row[0].isoformat(),
    }


# ── 事件消费侧的事实回查与模型身份（ADR-025） ────────────────────────────────
#
# 事件只是**通知**：`material.upserted` 的 `payload_ref` 是受控引用，可编码文本仍在
# `observation.payload_jsonb` 里（ADR-010：控制面不传载荷）。因此消费侧必须能按引用回查到
# 与写侧同事务落下的那些事实行——回查不到不是"没数据"，而是写侧缺陷，调用方要显式失败。


def load_material(conn, *, material_unit_id: str, revision: int) -> dict | None:
    """按 `(material_unit_id, revision)` 读素材事实；不存在返回 None（由调用方判定性质）。"""
    row = conn.execute(
        "SELECT stream_id, start_ms, end_ms, status FROM material_unit "
        "WHERE material_unit_id=%s AND revision=%s",
        (material_unit_id, revision),
    ).fetchone()
    if row is None:
        return None
    return {
        "material_unit_id": material_unit_id,
        "revision": revision,
        "stream_id": row[0],
        "start_ms": row[1],
        "end_ms": row[2],
        "status": row[3],
    }


def load_observations(conn, *, material_unit_id: str, revision: int) -> list[dict]:
    """这个 revision 引用到的观测（含 `payload_jsonb`），按 observation_id 稳定排序。

    顺序写死是刻意的：同一份事实两次消费必须得到同一批 `embedding_id` 与同一条文本，
    否则"重跑得到同一份向量"这条幂等性就没有依据。
    """
    rows = conn.execute(
        "SELECT o.observation_id, o.modality, o.payload_jsonb, o.contract_bytes FROM "
        "material_observation mo "
        "JOIN observation o ON o.observation_id = mo.observation_id "
        "WHERE mo.material_unit_id=%s AND mo.revision=%s ORDER BY o.observation_id",
        (material_unit_id, revision),
    ).fetchall()
    return [
        {
            "observation_id": row[0],
            "modality": row[1],
            "payload": dict(row[2]),
            "contract_bytes": row[3],
        }
        for row in rows
    ]


def ensure_model_release(
    conn,
    *,
    model_release_id: str,
    name: str,
    version: str,
    artifact_hash: str,
    backend: str,
    config_hash: str,
) -> None:
    """登记模型身份（`embedding_record.model_release_id` 的外键指向它）。

    存在就**校验一致**：同一个 release id 对应两份不同的身份，说明有人改了权重却复用了旧身份，
    那是身份体系的缺陷。这种情况显式冲突，不覆盖、也不"用最新的一份算了"。
    """
    conn.execute(
        "INSERT INTO model_release("
        "model_release_id,name,version,artifact_hash,backend,config_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (model_release_id) DO NOTHING",
        (model_release_id, name, version, artifact_hash, backend, config_hash),
    )
    row = conn.execute(
        "SELECT name,version,artifact_hash,backend,config_hash FROM model_release "
        "WHERE model_release_id=%s",
        (model_release_id,),
    ).fetchone()
    if row != (name, version, artifact_hash, backend, config_hash):
        raise IdentityConflict("model_release_identity_conflict")
