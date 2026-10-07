"""元数据 adapter。读取操作总会再水合出版本化的 protobuf 事实。

The append entry point is for an authorized timeline worker, never public HTTP.
"""

import hashlib

from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from edge_material_sdk.generated.material.v1.material_pb2 import MaterialUnit, Observation
from edge_material_sdk.manifest import declared_text, validate_payload
from edge_material_sdk.validation import validate_material
from google.protobuf.json_format import MessageToDict
from psycopg.types.json import Jsonb


class RevisionConflict(ValueError):
    pass


def append_derived_material(conn, material, *, trace_id, execution_id):
    """从本执行事实派生新版本；全局串行取号，自动推进版本链与历史血缘。"""
    return append_material(
        conn, material, trace_id=trace_id, execution_id=execution_id, auto_forward=True
    )


def append_material(
    conn,
    material: MaterialUnit,
    *,
    trace_id: str,
    execution_id: str = "",
    auto_forward: bool = False,
    forward_revision: bool | None = None,
) -> bool:
    """原子性事实 + lineage + outbox；True=新增，False=完全一致的 replay。

    若 auto_forward 为 True（或 forward_revision 为 True）：
      - 内容变化时受控自动递增升版至 expected (max+1)，并记录 prev_revision 前序血缘；
    若 auto_forward 为 False：
      - 严格校验 revision 连续性；对已有 revision 写入不同内容时抛出
        immutable_revision_conflict。
    """
    validate_material(material)
    if not trace_id:
        raise ValueError("missing_trace_id")
    if material.superseded:
        raise ValueError("superseded_is_read_only")
    if forward_revision is not None:
        auto_forward = forward_revision

    data = material.SerializeToString(deterministic=True)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    with conn.transaction():
        if (
            execution_id
            and not conn.execute(
                "SELECT 1 FROM console_job_execution WHERE execution_id=%s",
                (execution_id,),
            ).fetchone()
        ):
            raise ValueError("unknown_execution")
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (material.material_unit_id,)
        )
        previous = conn.execute(
            "SELECT revision, content_hash, stream_id, prev_revision FROM material_unit "
            "WHERE material_unit_id=%s ORDER BY revision DESC",
            (material.material_unit_id,),
        ).fetchall()
        for _rev, _hash, stream_id, _prev in previous:
            if stream_id != material.stream_id:
                raise RevisionConflict("material_stream_changed")

        latest_rev = previous[0][0] if previous else 0
        latest_hash = previous[0][1] if previous else None
        expected = latest_rev + 1

        # 检查是否为对某一已有版本的完全重放 (Idempotent Replay)
        for revision, content_hash, _stream_id, _prev in previous:
            if revision == material.revision:
                if digest == content_hash:
                    if execution_id:
                        conn.execute(
                            """
                            INSERT INTO material_execution(
                                material_unit_id,material_revision,execution_id
                            ) VALUES (%s,%s,%s)
                            ON CONFLICT DO NOTHING
                            """,
                            (material.material_unit_id, material.revision, execution_id),
                        )
                    return False
                # 同一 revision 内容不同：若未显式允许升版，严禁覆盖历史不可变事实
                if not auto_forward:
                    raise RevisionConflict("immutable_revision_conflict")

        if auto_forward:
            if previous:
                # 检查内容是否与最新版实质一致（若传入未升版旧 revision，需调整后比对 hash）
                candidate = MaterialUnit()
                candidate.CopyFrom(material)
                candidate.revision = latest_rev
                if previous[0][3] is not None:
                    candidate.prev_revision = previous[0][3]
                else:
                    candidate.ClearField("prev_revision")
                candidate_data = candidate.SerializeToString(deterministic=True)
                candidate_digest = "sha256:" + hashlib.sha256(candidate_data).hexdigest()
                if candidate_digest == latest_hash:
                    material.CopyFrom(candidate)
                    if execution_id:
                        conn.execute(
                            """
                            INSERT INTO material_execution(
                                material_unit_id,material_revision,execution_id
                            ) VALUES (%s,%s,%s)
                            ON CONFLICT DO NOTHING
                            """,
                            (material.material_unit_id, material.revision, execution_id),
                        )
                    return False

                # 内容发生变更：受控自动升版并推进历史血缘
                material.revision = expected
                material.prev_revision = latest_rev
                data = material.SerializeToString(deterministic=True)
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
            else:
                material.revision = 1
                material.ClearField("prev_revision")
                data = material.SerializeToString(deterministic=True)
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
        else:
            if material.revision == 0:
                if previous:
                    material.revision = expected
                    material.prev_revision = latest_rev
                else:
                    material.revision = 1
                    material.ClearField("prev_revision")
                data = material.SerializeToString(deterministic=True)
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
            else:
                if material.revision != expected:
                    raise RevisionConflict("non_sequential_revision")
                if previous:
                    if not material.HasField("prev_revision") or material.prev_revision == 0:
                        material.prev_revision = latest_rev
                    else:
                        known_revs = {r[0] for r in previous}
                        if material.prev_revision not in known_revs:
                            raise RevisionConflict("invalid_prev_revision")
                    data = material.SerializeToString(deterministic=True)
                    digest = "sha256:" + hashlib.sha256(data).hexdigest()
                else:
                    material.ClearField("prev_revision")
                    data = material.SerializeToString(deterministic=True)
                    digest = "sha256:" + hashlib.sha256(data).hexdigest()
        source = conn.execute(
            "SELECT source_id FROM stream_session WHERE stream_id=%s", (material.stream_id,)
        ).fetchone()
        if not source:
            raise ValueError("unknown_stream")
        for ref in material.source_refs:
            asset = conn.execute(
                "SELECT stream_id, sha256, duration_ms FROM media_asset WHERE asset_id=%s",
                (ref.asset_id,),
            ).fetchone()
            if not asset or asset[:2] != (material.stream_id, ref.content_hash):
                raise ValueError("invalid_asset_reference")
            if ref.time_range.end_ms > asset[2]:
                raise ValueError("asset_time_range_exceeded")
        for obs in material.observations:
            if obs.source_id != source[0]:
                raise ValueError("source_stream_mismatch")
            p = obs.provenance
            if p.processor_release_id:
                registration = _registered_observation(conn, obs)
            else:
                registration = None
            release = conn.execute(
                "SELECT name,artifact_hash FROM model_release WHERE model_release_id=%s",
                (p.model_release_id,),
            ).fetchone()
            if p.model_applicability != 2 and (
                not release or (release[0], release[1]) != (p.model_id, p.model_artifact_digest)
            ):
                raise ValueError("model_release_mismatch")
            item = conn.execute(
                "SELECT stream_id,start_ms,end_ms FROM timeline_item WHERE item_id=%s",
                (obs.source_item_id,),
            ).fetchone()
            if (
                not item
                or item[0] != material.stream_id
                or obs.time_range.start_ms < item[1]
                or obs.time_range.end_ms > item[2]
            ):
                raise ValueError("timeline_item_mismatch")
            obs_data = obs.SerializeToString(deterministic=True)
            saved = conn.execute(
                "SELECT contract_bytes FROM observation WHERE observation_id=%s",
                (obs.observation_id,),
            ).fetchone()
            if saved is not None:
                saved_obs = Observation()
                saved_obs.ParseFromString(saved[0])
                saved_dict = MessageToDict(saved_obs.payload)
                curr_dict = MessageToDict(obs.payload)
                saved_content = saved_dict.get("blocks") or saved_dict.get("text")
                curr_content = curr_dict.get("blocks") or curr_dict.get("text")
                if (
                    saved_content == curr_content
                    and saved_obs.content_hash == obs.content_hash
                    and saved_obs.source_item_id == obs.source_item_id
                    and saved_obs.modality == obs.modality
                    and (
                        not obs.schema_digest
                        or (
                            saved_dict == curr_dict
                            and saved_obs.schema_digest == obs.schema_digest
                            and saved_obs.schema_id == obs.schema_id
                            and saved_obs.schema_version == obs.schema_version
                            and saved_obs.provenance == obs.provenance
                            and saved_obs.time_range == obs.time_range
                        )
                    )
                ):
                    obs.CopyFrom(saved_obs)
                    obs_data = saved[0]
                else:
                    raise RevisionConflict("immutable_observation_conflict")
            else:
                conn.execute(
                    "INSERT INTO observation VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (observation_id) DO NOTHING",
                    (
                        obs.observation_id,
                        obs.source_item_id,
                        obs.modality,
                        Jsonb(MessageToDict(obs.payload)),
                        obs.confidence if obs.HasField("confidence") else None,
                        p.model_release_id or None,
                        obs_data,
                    ),
                )
        text_parts = []
        for obs in material.observations:
            if obs.schema_digest:
                registration = _registered_observation(conn, obs)
                declaration = next(
                    value
                    for value in registration["manifest"]["spec"]["outputs"]
                    if value["modality"] == obs.modality
                )
                text_parts.append(
                    declared_text(MessageToDict(obs.payload), declaration.get("textFields", []))
                )
            else:
                text_parts.extend(_payload_text(MessageToDict(obs.payload)))
        text = "\n".join(part for part in text_parts if part)
        prev_rev = material.prev_revision if material.HasField("prev_revision") else None
        conn.execute(
            "INSERT INTO material_unit(material_unit_id,revision,stream_id,start_ms,end_ms,"
            "status,tags,search_text,contract_bytes,content_hash,prev_revision) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                material.material_unit_id,
                material.revision,
                material.stream_id,
                material.time_range.start_ms,
                material.time_range.end_ms,
                material.status,
                list(material.tags),
                text,
                data,
                digest,
                prev_rev,
            ),
        )
        for obs in material.observations:
            conn.execute(
                "INSERT INTO material_observation VALUES (%s,%s,%s,%s)",
                (material.material_unit_id, material.revision, obs.observation_id, obs.modality),
            )
        for ref in material.source_refs:
            conn.execute(
                "INSERT INTO material_source_reference VALUES (%s,%s,%s,%s,%s)",
                (
                    material.material_unit_id,
                    material.revision,
                    ref.asset_id,
                    ref.time_range.start_ms,
                    ref.time_range.end_ms,
                ),
            )
        if execution_id:
            conn.execute(
                """
                INSERT INTO material_execution(material_unit_id,material_revision,execution_id)
                VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (material.material_unit_id, material.revision, execution_id),
            )
        event_id = f"material:{material.material_unit_id}:{material.revision}"
        event = EventEnvelope(
            event_id=event_id,
            event_type="material.upserted",
            stream_id=material.stream_id,
            trace_id=trace_id,
            payload_ref=event_id,
            created_at_unix_ms=material.created_at_unix_ms,
            schema_version=1,
        )
        updated = conn.execute(
            (
                "UPDATE event_outbox SET contract_bytes=%s, event_type=%s "
                "WHERE event_id=%s RETURNING event_id"
            ),
            (event.SerializeToString(deterministic=True), event.event_type, event_id),
        ).fetchone()
        if not updated:
            conn.execute(
                "INSERT INTO event_outbox(event_id,event_type,contract_bytes) VALUES (%s,%s,%s)",
                (event_id, event.event_type, event.SerializeToString(deterministic=True)),
            )
        return True


def _registered_observation(conn, observation):
    """schema 与处理器身份只能由不可变 release 注册记录解析。"""
    from ..contracts import one

    p = observation.provenance
    row = one(
        conn,
        "SELECT r.plugin_id,r.plugin_version,r.artifact_digest,g.manifest,g.schemas "
        "FROM plugin_release r JOIN plugin_registration g USING(release_id) "
        "WHERE r.release_id=%s",
        (p.processor_release_id,),
    )
    if not row or (p.plugin, p.plugin_version, p.artifact_digest) != (
        row["plugin_id"],
        row["plugin_version"],
        row["artifact_digest"],
    ):
        raise ValueError("processor_release_mismatch")
    expected = 2 if row["manifest"]["spec"]["modelApplicability"] == "not_applicable" else 1
    if p.model_applicability != expected:
        raise ValueError("processor_model_applicability_mismatch")
    validate_payload(observation, row["manifest"]["spec"]["outputs"], row["schemas"])
    return row


def _payload_text(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _payload_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_text(item)


BASE = """FROM material_unit m
    JOIN stream_session s ON s.stream_id=m.stream_id
    JOIN media_source source ON source.source_id=s.source_id"""
LATEST = """NOT EXISTS (SELECT 1 FROM material_unit newer
    WHERE newer.material_unit_id=m.material_unit_id AND newer.revision>m.revision)"""
LATEST_EXECUTION = """NOT EXISTS (SELECT 1 FROM material_unit newer
    JOIN material_execution e ON (e.material_unit_id,e.material_revision)=
    (newer.material_unit_id,newer.revision) WHERE e.execution_id=%s
    AND newer.material_unit_id=m.material_unit_id AND newer.revision>m.revision)"""


def get_material(conn, principal, material_id, revision=None, execution_id: str = ""):
    clause = "m.revision=%s" if revision is not None else LATEST
    params = [principal, material_id] + ([revision] if revision is not None else [])
    if revision is None and execution_id:
        clause = LATEST_EXECUTION
        params.append(execution_id)
    execution_clause = ""
    if execution_id:
        execution_clause = (
            " AND EXISTS (SELECT 1 FROM material_execution execution "
            "WHERE execution.material_unit_id=m.material_unit_id "
            "AND execution.material_revision=m.revision AND execution.execution_id=%s)"
        )
        params.append(execution_id)
    row = conn.execute(
        f"SELECT m.contract_bytes, NOT ({LATEST}), m.prev_revision {BASE} "
        f"WHERE source.owner=%s AND m.material_unit_id=%s AND {clause}{execution_clause}",
        params,
    ).fetchone()
    if row is None:
        return None
    result = MaterialUnit.FromString(row[0])
    result.superseded = row[1]
    if row[2] is not None:
        result.prev_revision = row[2]
    return result


def search_materials(conn, principal, request):
    clauses = ["source.owner=%s", LATEST, "m.status <> 'failed'"]
    params = [principal]
    if request.execution_id:
        clauses[1] = LATEST_EXECUTION
        params.append(request.execution_id)
    if request.query:
        # 字面子串查询：'%' 与 '_' 不得变成通配符查询。
        clauses.append("strpos(lower(m.search_text), lower(%s)) > 0")
        params.append(request.query)
    if request.stream_id:
        clauses.append("m.stream_id=%s")
        params.append(request.stream_id)
    if request.execution_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM material_execution execution "
            "WHERE execution.material_unit_id=m.material_unit_id "
            "AND execution.material_revision=m.revision AND execution.execution_id=%s)"
        )
        params.append(request.execution_id)
    if request.HasField("start_ms"):
        clauses.append("m.end_ms>%s")
        params.append(request.start_ms)
    if request.HasField("end_ms"):
        clauses.append("m.start_ms<%s")
        params.append(request.end_ms)
    if request.tags:
        clauses.append("m.tags @> %s::text[]")
        params.append(list(request.tags))
    if request.modalities or request.HasField("min_confidence"):
        obs_clauses = ["mo.material_unit_id=m.material_unit_id", "mo.revision=m.revision"]
        if request.modalities:
            obs_clauses.append("o.modality=ANY(%s)")
            params.append(list(request.modalities))
        if request.HasField("min_confidence"):
            obs_clauses.append("o.confidence >= %s")
            params.append(request.min_confidence)
        clauses.append(
            "EXISTS (SELECT 1 FROM material_observation mo JOIN observation o "
            "ON o.observation_id=mo.observation_id WHERE " + " AND ".join(obs_clauses) + ")"
        )
    params.append(request.limit or 20)
    rows = conn.execute(
        f"SELECT m.contract_bytes, m.prev_revision {BASE} WHERE {' AND '.join(clauses)} "
        "ORDER BY m.start_ms DESC, m.material_unit_id LIMIT %s",
        params,
    ).fetchall()
    results = []
    for row in rows:
        unit = MaterialUnit.FromString(row[0])
        if row[1] is not None:
            unit.prev_revision = row[1]
        results.append(unit)
    return results


def get_material_lineage(conn, principal: str, material_id: str) -> list[dict]:
    """返回素材版本链与历史快照追溯列表（按 revision 升序）。"""
    rows = conn.execute(
        f"""
        SELECT m.revision, m.prev_revision, m.status, m.content_hash,
               EXTRACT(EPOCH FROM m.created_at) * 1000 AS created_at_ms,
               NOT ({LATEST}) AS superseded, m.tags, m.contract_bytes
        {BASE}
        WHERE source.owner=%s AND m.material_unit_id=%s
        ORDER BY m.revision ASC
        """,
        (principal, material_id),
    ).fetchall()
    items = []
    for row in rows:
        unit = MaterialUnit.FromString(row[7])
        items.append(
            {
                "revision": row[0],
                "prev_revision": row[1],
                "status": row[2],
                "content_hash": row[3],
                "created_at_unix_ms": int(row[4]) if row[4] is not None else None,
                "superseded": bool(row[5]),
                "tags": list(row[6] or []),
                "pipeline_version": unit.pipeline_version or "",
                "observations_count": len(unit.observations),
            }
        )
    return items
