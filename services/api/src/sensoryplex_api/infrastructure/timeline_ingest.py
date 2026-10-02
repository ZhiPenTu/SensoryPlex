"""Agent 回传 Timeline 融合事实的受控写侧。

节点只能提交 Runtime 已生成的派生描述、Timeline item 与 MaterialUnit protobuf；这个模块
在控制面内登记引用、模型版本与素材事实。它不接受数据库 URL、宿主路径或原始媒体字节。
"""

from __future__ import annotations

from datetime import UTC, datetime

from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.media.v1 import media_pb2 as media


class TimelineIngestError(ValueError):
    """可安全展示给 Agent/Console 的稳定写侧拒绝码。"""


SOURCE_TYPES = {
    media.MEDIA_SOURCE_KIND_FILE: "file",
    media.MEDIA_SOURCE_KIND_SRT: "srt",
}


def _same_or_record(failures: list[str], existing, expected, code: str) -> None:
    if existing is not None and existing != expected:
        failures.append(code)


def register_references(
    conn,
    *,
    description: media.MediaSourceDescription,
    owner: str,
    units: list[material.MaterialUnit],
    timeline_items: dict[str, tuple[str, int, int]],
    upload_id: str,
) -> dict[str, int]:
    """从 Runtime 描述和融合结果登记可复核的引用事实。

    `timeline_items` 必须是 Runtime descriptor 帐本的视图。这里不从模型 Observation
    "猜"一个 buffer 窗口：每个 Observation 都要命中该帐本中的同一 source_item_id。
    """
    if not owner or not upload_id:
        raise TimelineIngestError("timeline_ingest_input_invalid")
    source = description.source
    source_type = SOURCE_TYPES.get(source.kind)
    if (
        not source_type
        or not source.stream_id
        or not source.source_id
        or not source.content_hash.startswith("sha256:")
        or description.duration_ms <= 0
    ):
        raise TimelineIngestError("timeline_source_description_invalid")
    if not description.tracks:
        raise TimelineIngestError("timeline_source_tracks_missing")
    if any(unit.stream_id != source.stream_id for unit in units):
        raise TimelineIngestError("timeline_material_stream_mismatch")

    for unit in units:
        if not unit.material_unit_id or unit.revision <= 0:
            raise TimelineIngestError("timeline_material_identity_invalid")
        for reference in unit.source_refs:
            if (
                reference.asset_id != f"asset-{source.content_hash[7:19]}"
                or reference.content_hash != source.content_hash
                or reference.time_range.end_ms > description.duration_ms
                or reference.time_range.end_ms <= reference.time_range.start_ms
            ):
                raise TimelineIngestError("timeline_material_source_reference_invalid")
        for observation in unit.observations:
            item = timeline_items.get(observation.source_item_id)
            if (
                observation.stream_id != source.stream_id
                or observation.source_id != source.source_id
                or item is None
                or observation.time_range.start_ms < item[1]
                or observation.time_range.end_ms > item[2]
            ):
                raise TimelineIngestError("timeline_observation_item_mismatch")

    track_codecs = ",".join(
        f"{track.track_kind}:{track.codec}" for track in description.tracks if track.track_kind
    )
    if not track_codecs:
        raise TimelineIngestError("timeline_source_tracks_invalid")
    if units:
        earliest = min(unit.created_at_unix_ms for unit in units)
        if earliest <= 0:
            raise TimelineIngestError("timeline_material_created_at_invalid")
        started_at = datetime.fromtimestamp(earliest / 1000, tz=UTC)
    else:
        # 无 Observation 的执行仍须登记来源并写完整 coverage；媒体协议没有录制开始墙钟，
        # 因而此处的 session 时间是控制面首次登记该可信 source 的时刻，而非伪造媒体时间。
        started_at = datetime.now(UTC)

    failures: list[str] = []
    _same_or_record(
        failures,
        conn.execute(
            "SELECT type,uri_redacted,owner FROM media_source WHERE source_id=%s",
            (source.source_id,),
        ).fetchone(),
        (source_type, "private://not-exposed", owner),
        "media_source_conflict",
    )
    conn.execute(
        """
        INSERT INTO media_source(source_id,type,uri_redacted,owner)
        VALUES (%s,%s,'private://not-exposed',%s) ON CONFLICT (source_id) DO NOTHING
        """,
        (source.source_id, source_type, owner),
    )
    _same_or_record(
        failures,
        conn.execute(
            "SELECT source_id FROM stream_session WHERE stream_id=%s", (source.stream_id,)
        ).fetchone(),
        (source.source_id,),
        "stream_session_conflict",
    )
    conn.execute(
        """
        INSERT INTO stream_session(stream_id,source_id,started_at,status)
        VALUES (%s,%s,%s,'stopped') ON CONFLICT (stream_id) DO NOTHING
        """,
        (source.stream_id, source.source_id, started_at),
    )
    asset_id = f"asset-{source.content_hash[7:19]}"
    _same_or_record(
        failures,
        conn.execute(
            "SELECT stream_id,sha256,codec,duration_ms FROM media_asset WHERE asset_id=%s",
            (asset_id,),
        ).fetchone(),
        (source.stream_id, source.content_hash, track_codecs, description.duration_ms),
        "media_asset_conflict",
    )
    conn.execute(
        """
        INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (asset_id) DO UPDATE SET object_uri=EXCLUDED.object_uri
        """,
        (
            asset_id,
            source.stream_id,
            f"upload://{upload_id}",
            source.content_hash,
            track_codecs,
            description.duration_ms,
        ),
    )
    for item_id, (kind, start_ms, end_ms) in timeline_items.items():
        _same_or_record(
            failures,
            conn.execute(
                "SELECT stream_id,kind,start_ms,end_ms FROM timeline_item WHERE item_id=%s",
                (item_id,),
            ).fetchone(),
            (source.stream_id, kind, start_ms, end_ms),
            "timeline_item_conflict",
        )
        conn.execute(
            """
            INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING
            """,
            (item_id, source.stream_id, kind, start_ms, end_ms),
        )
    releases: dict[str, tuple[str, str, str, str, str]] = {}
    for unit in units:
        for observation in unit.observations:
            p = observation.provenance
            if p.model_applicability == material.MODEL_APPLICABILITY_NOT_APPLICABLE:
                from .materials import _registered_observation

                _registered_observation(conn, observation)
                continue
            releases[p.model_release_id] = (
                p.model_id,
                p.model_version,
                p.model_artifact_digest,
                p.execution_backend,
                p.config_hash,
            )
    for release_id, expected in releases.items():
        if not release_id or not all(expected):
            raise TimelineIngestError("timeline_model_provenance_invalid")
        _same_or_record(
            failures,
            conn.execute(
                """
                SELECT name,artifact_hash
                FROM model_release WHERE model_release_id=%s
                """,
                (release_id,),
            ).fetchone(),
            (expected[0], expected[2]),
            "model_release_conflict",
        )
        conn.execute(
            """
            INSERT INTO model_release(
                model_release_id,name,version,artifact_hash,backend,config_hash
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (model_release_id) DO NOTHING
            """,
            (release_id, *expected),
        )
    if failures:
        raise TimelineIngestError("reference_fact_conflict:" + ",".join(sorted(set(failures))))
    return {
        "media_sources": 1,
        "stream_sessions": 1,
        "media_assets": 1,
        "timeline_items": len(timeline_items),
        "model_releases": len(releases),
    }
