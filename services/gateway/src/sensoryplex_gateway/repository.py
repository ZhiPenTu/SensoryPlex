"""Metadata adapter. Reads always rehydrate the versioned protobuf fact.

The append entry point is for an authorized timeline worker, never public HTTP.
"""

import hashlib

from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from edge_material_sdk.generated.material.v1.material_pb2 import MaterialUnit
from edge_material_sdk.validation import validate_material
from google.protobuf.json_format import MessageToDict
from psycopg.types.json import Jsonb


class RevisionConflict(ValueError):
    pass


def append_material(conn, material: MaterialUnit, *, trace_id: str) -> bool:
    """Atomic fact + lineage + outbox; True=new, False=identical replay."""
    validate_material(material)
    if not trace_id:
        raise ValueError("missing_trace_id")
    if material.superseded:
        raise ValueError("superseded_is_read_only")
    data = material.SerializeToString(deterministic=True)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (material.material_unit_id,)
        )
        previous = conn.execute(
            "SELECT revision, content_hash, stream_id FROM material_unit "
            "WHERE material_unit_id=%s ORDER BY revision DESC",
            (material.material_unit_id,),
        ).fetchall()
        for revision, content_hash, stream_id in previous:
            if stream_id != material.stream_id:
                raise RevisionConflict("material_stream_changed")
            if revision == material.revision:
                if digest == content_hash:
                    return False
                raise RevisionConflict("immutable_revision_conflict")
        expected = previous[0][0] + 1 if previous else 1
        if material.revision != expected:
            raise RevisionConflict("non_sequential_revision")
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
            release = conn.execute(
                "SELECT name,version,artifact_hash,backend,config_hash FROM model_release "
                "WHERE model_release_id=%s",
                (p.model_release_id,),
            ).fetchone()
            if release != (
                p.model_id,
                p.model_version,
                p.model_artifact_digest,
                p.execution_backend,
                p.config_hash,
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
            conn.execute(
                "INSERT INTO observation VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (observation_id) DO NOTHING",
                (
                    obs.observation_id,
                    obs.source_item_id,
                    obs.modality,
                    Jsonb(MessageToDict(obs.payload)),
                    obs.confidence if obs.HasField("confidence") else None,
                    p.model_release_id,
                    obs_data,
                ),
            )
            saved = conn.execute(
                "SELECT contract_bytes FROM observation WHERE observation_id=%s",
                (obs.observation_id,),
            ).fetchone()[0]
            if saved != obs_data:
                raise RevisionConflict("immutable_observation_conflict")
        text = "\n".join(
            part
            for obs in material.observations
            for part in _payload_text(MessageToDict(obs.payload))
        )
        conn.execute(
            "INSERT INTO material_unit(material_unit_id,revision,stream_id,start_ms,end_ms,"
            "status,tags,search_text,contract_bytes,content_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
        conn.execute(
            "INSERT INTO event_outbox(event_id,event_type,contract_bytes) VALUES (%s,%s,%s)",
            (event_id, event.event_type, event.SerializeToString(deterministic=True)),
        )
    return True


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


def get_material(conn, principal, material_id, revision=None):
    clause = "m.revision=%s" if revision is not None else LATEST
    params = [principal, material_id] + ([revision] if revision is not None else [])
    row = conn.execute(
        f"SELECT m.contract_bytes, NOT ({LATEST}) {BASE} "
        f"WHERE source.owner=%s AND m.material_unit_id=%s AND {clause}",
        params,
    ).fetchone()
    if row is None:
        return None
    result = MaterialUnit.FromString(row[0])
    result.superseded = row[1]
    return result


def search_materials(conn, principal, request):
    clauses = ["source.owner=%s", LATEST, "m.status <> 'failed'"]
    params = [principal]
    if request.query:
        # Literal substring search: '%' and '_' must not become wildcard queries.
        clauses.append("strpos(lower(m.search_text), lower(%s)) > 0")
        params.append(request.query)
    if request.stream_id:
        clauses.append("m.stream_id=%s")
        params.append(request.stream_id)
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
        f"SELECT m.contract_bytes {BASE} WHERE {' AND '.join(clauses)} "
        "ORDER BY m.start_ms DESC, m.material_unit_id LIMIT %s",
        params,
    ).fetchall()
    return [MaterialUnit.FromString(row[0]) for row in rows]
